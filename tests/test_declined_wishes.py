"""Run receipt explanations, canonical wish semantics and bounded department-scale work."""
import copy
import time
import uuid
from datetime import date, timedelta

import pytest

from packages.contracts.schemas import DeclinedWish, JobResult
from packages.domain.cp_sat import generate
from packages.domain.patterns import wish_evaluations, wish_outcomes
from packages.domain.validation import evaluate
from packages.domain.wish_reasons import declined_wishes
from tests.test_staff_wishes import wish


def test_declined_receipt_names_blocking_rule_without_private_absence_text(h):
    person = h.seed['people'][0]
    rule = h.policy({'kind': 'restriction', 'weekdays': [6], 'start_time': '00:00', 'end_time': '24:00'},
                    {'person_id': person})
    row = wish(h, '2026-10-04', kind='prefer_on')['wish']
    workspace = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(workspace)
    result = generate(data, 5)
    reason = next(w for w in result['declined_wishes'] if w['wish_id'] == row['id'])['reasons'][0]
    assert reason['kind'] == 'rule' and reason['rule_id'] == rule['id'] and reason['rule_name'] == rule['name']
    assert rule['name'] in reason['message'] and reason['shift_id'] == data['shifts'][0]['id']
    assert JobResult.model_validate(result).declined_wishes


def test_person_local_probe_uses_canonical_rest_rule(h):
    person = h.seed['people'][0]
    rule = h.policy({'kind': 'min_rest', 'hours': 24})
    wish(h, '2026-10-05', kind='prefer_on')
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00',
                        end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(workspace)
    original = copy.deepcopy(data)
    reason = declined_wishes(data, evaluate(data))[0]['reasons'][0]
    assert reason['kind'] == 'rule' and reason['code'] == 'MIN_REST' and reason['rule_name'] == rule['name']
    assert data == original


def test_cover_reason_and_partly_declined_days_share_the_outcome_evaluator(h):
    person = h.seed['people'][0]
    own = wish(h, '2026-10-04', last='2026-10-05')['wish']
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    data = h.snapshot(workspace)
    receipt = declined_wishes(data, evaluate(data))[0]
    assert receipt['wish_id'] == own['id'] and receipt['honoured'] == 0.5
    assert wish_outcomes(data)[own['id']] == (0.5, 2)
    assert len(receipt['reasons']) == 1
    reason = receipt['reasons'][0]
    assert reason['day'] == '2026-10-04' and reason['kind'] == 'cover'
    assert reason['shift_id'] == data['shifts'][0]['id'] and reason['session_label']
    assert DeclinedWish.model_validate(receipt)


@pytest.mark.parametrize('kind', ['prefer_off', 'avoid_shift_type'])
@pytest.mark.parametrize('extra_cover,unfilled,expected', [
    (False, 'open', 'cover'), (True, 'gap', 'cover'), (True, 'open', 'tradeoff'),
])
def test_optional_extra_cover_is_not_reported_as_required(h, kind, extra_cover, unfilled, expected):
    person = h.seed['people'][0]
    typ = h.configure('assignment_types', {'name': 'Extra-cover receipt', 'starts_on': '2020-01-01',
        'definition': {'extra_cover': extra_cover, 'extra_cover_unfilled': unfilled,
                       'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    extra = {'assignment_type_id': typ} if kind == 'avoid_shift_type' else {}
    row = wish(h, '2026-10-04', kind=kind, **extra)['wish']
    workspace = h.shift(h.workspace(), typ=typ, people=[{'person_id': person, 'overtime': True,
        'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00'}])['workspace']
    data = h.snapshot(workspace)
    checked = evaluate(data)
    assert checked['valid'], checked['findings']
    empty = copy.deepcopy(data)
    empty['shifts'][0]['participants'] = []
    assert evaluate(empty)['valid'] is (expected == 'tradeoff')
    receipt = next(r for r in declined_wishes(data, checked) if r['wish_id'] == row['id'])
    assert receipt['reasons'][0]['kind'] == expected
    publication = h.publish(workspace)['publication_id']
    snapshot = h.get('coordinator', '/api/publications/' + publication)['snapshot']
    frozen = next(w for w in snapshot['wishes'] if w['id'] == row['id'])
    assert frozen['outcome_reasons'] == receipt['reasons']
    staff = next(w for w in h.get('staff', '/api/wishes')['wishes'] if w['id'] == row['id'])
    assert staff['outcome_reasons'][0]['kind'] == expected


@pytest.mark.parametrize('unavailable', [False, True])
def test_cover_reason_counts_only_the_persons_eligible_presence(h, unavailable):
    person, other = h.seed['people'][:2]
    wish(h, '2026-10-04')
    workspace = h.shift(h.workspace(), people=[person, other])['workspace']
    data = h.snapshot(workspace)
    shift = data['shifts'][0]
    participant = next(p for p in shift['participants'] if p['person_id'] == person)
    if unavailable:
        data['availability'].append({'id': 'synthetic-absence', 'person_id': person,
                                     'start': participant['start'], 'end': participant['end']})
    else:
        participant['segments'] = [{'start': participant['start'], 'end': '2026-10-04T09:00:00+03:00'}]
    # The other person covers the minimum alone; the declined person's envelope is not evidence of cover.
    reasons = declined_wishes(data, evaluate(data))[0]['reasons']
    assert all(reason['kind'] != 'cover' for reason in reasons)


def test_competing_wishes_show_effective_daily_weights_and_winner(h):
    person, other = h.seed['people'][:2]
    lower = wish(h, '2026-10-04', kind='prefer_on', points=1)['wish']
    higher = wish(h, '2026-10-04', kind='prefer_on', points=4, person=other, role='coordinator')['wish']
    # A granted second item keeps point priorities distinct in each person's normalized preference budget.
    wish(h, '2026-10-05', kind='prefer_off', points=1)
    wish(h, '2026-10-05', kind='prefer_off', points=1, person=other, role='coordinator')
    typ = h.configure('assignment_types', {'name': 'One place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace(), people=[], typ=typ)['workspace']
    data = h.snapshot(workspace)
    data['people'] = {pid: data['people'][pid] for pid in (person, other)}
    result = generate(data, 10)
    assert result['status'] == 'candidate_feasible'
    receipt = next(w for w in result['declined_wishes'] if w['wish_id'] == lower['id'])
    reason = receipt['reasons'][0]
    assert reason['kind'] == 'competing_wishes'
    winner, = reason['competing_wishes']
    assert winner['wish_id'] == higher['id'] and winner['person_id'] == other
    assert winner['person_name'] == data['people'][other]['display_name']
    assert receipt['weight'] == 0.1 and winner['weight'] == winner['daily_weight'] == 0.4
    assert DeclinedWish.model_validate(receipt)


@pytest.mark.parametrize('kind', ['prefer_on', 'prefer_shift_type'])
@pytest.mark.parametrize('maximum,expected', [(1, 'competing_wishes'), (2, 'tradeoff'), (None, 'tradeoff')])
def test_honoured_incumbent_is_only_competing_when_capacity_requires_replacement(h, kind, maximum, expected):
    person, other = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Capacity evidence', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': maximum}]}})['record']['id']
    extra = {'assignment_type_id': typ} if kind == 'prefer_shift_type' else {}
    lower = wish(h, '2026-10-04', kind=kind, points=1, **extra)['wish']
    wish(h, '2026-10-04', kind=kind, points=4, person=other, role='coordinator', **extra)
    workspace = h.shift(h.workspace(), people=[other], typ=typ)['workspace']
    if kind == 'prefer_shift_type':
        # A type wish is neutral on a day off; a different duty makes this one genuinely unmet.
        workspace = h.shift(workspace, start='2026-10-04T16:00:00+03:00',
                            end='2026-10-04T20:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(workspace)
    original = copy.deepcopy(data)
    receipt = next(row for row in declined_wishes(data, evaluate(data)) if row['wish_id'] == lower['id'])
    assert receipt['reasons'][0]['kind'] == expected
    assert data == original


@pytest.mark.parametrize('kind', ['prefer_on', 'prefer_shift_type'])
def test_incumbents_wish_is_not_displaced_when_another_duty_still_honours_it(h, kind):
    person, other = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'One place with another duty', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    extra = {'assignment_type_id': typ} if kind == 'prefer_shift_type' else {}
    lower = wish(h, '2026-10-04', kind=kind, points=1, **extra)['wish']
    wish(h, '2026-10-04', kind=kind, points=4, person=other, role='coordinator', **extra)
    workspace = h.shift(h.workspace(), people=[other], typ=typ)['workspace']
    workspace = h.shift(workspace, start='2026-10-04T12:00:00+03:00', end='2026-10-04T16:00:00+03:00',
                        people=[other], typ=typ)['workspace']
    if kind == 'prefer_shift_type':
        # A type wish is neutral on a day off; a different duty makes this one genuinely unmet.
        workspace = h.shift(workspace, start='2026-10-04T16:00:00+03:00',
                            end='2026-10-04T20:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(workspace)
    receipt = next(row for row in declined_wishes(data, evaluate(data)) if row['wish_id'] == lower['id'])
    assert receipt['reasons'][0]['kind'] == 'tradeoff'


@pytest.mark.parametrize('minimum,expected', [(0, 'tradeoff'), (1, 'cover')])
def test_declined_off_wish_reports_only_verified_cover_evidence(h, minimum, expected):
    person, other = h.seed['people'][:2]
    lower = wish(h, '2026-10-04', kind='prefer_off', points=1)['wish']
    wish(h, '2026-10-04', kind='prefer_off', points=4, person=other, role='coordinator')
    typ = h.configure('assignment_types', {'name': 'Required cover evidence', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': minimum, 'maximum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace(), people=[person], typ=typ)['workspace']
    data = h.snapshot(workspace)
    evidence = [{'shift_id': data['shifts'][0]['id'], 'person_id': other, 'exclusions': []}]
    receipt = next(row for row in declined_wishes(data, evaluate(data), evidence) if row['wish_id'] == lower['id'])
    reason = receipt['reasons'][0]
    assert reason['kind'] == expected
    assert 'competing_wishes' not in reason


def test_unknown_tradeoff_and_cancelled_sessions_do_not_invent_a_blocker(h):
    wish(h, '2026-10-04', kind='prefer_on')
    workspace = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(workspace)
    data['shifts'][0]['cancelled'] = True
    reason = declined_wishes(data)[0]['reasons'][0]
    assert reason['kind'] == 'tradeoff' and 'no single blocking rule' in reason['message']
    assert 'shift_id' not in reason


def test_worker_receipt_survives_apply_review_publication_and_is_private_for_staff(h, monkeypatch):
    from dataclasses import replace
    from apps.api.auth import authenticate
    from apps.api.db import transaction
    from apps.api.services.jobs import create_job
    from packages.contracts.schemas import Command

    person, other = h.seed['people'][:2]
    lower = wish(h, '2026-10-04', kind='prefer_on')['wish']
    wish(h, '2026-10-04', kind='prefer_on', points=4, person=other, role='coordinator')
    typ = h.configure('assignment_types', {'name': 'One published place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace(), people=[], typ=typ)['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
                        people=[person], typ=typ)['workspace']
    client = h.client('coordinator')
    with transaction(h.app.state.engine, write=True) as db:
        actor = authenticate(db, client.cookies['rota_session'], client.cookies['rota_csrf'], write=True)
        job = create_job(db, actor, Command(kind='job.create', expected_revision=workspace['revision'], payload={
            'workspace_id': workspace['id'], 'kind': 'generation', 'generation_options': {'mode': 'fill_gaps'}}), replace(h.settings, generation_engine='cp_sat'))['job']
    h.process_jobs()
    result = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    receipt = next(row for row in result['declined_wishes'] if row['wish_id'] == lower['id'])
    assert receipt['reasons'][0]['kind'] == 'competing_wishes'
    applied = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
        'reason': 'Apply the reviewed preference result'}, revision=workspace['revision'])['workspace']
    from packages.domain import cp_sat
    monkeypatch.setattr(cp_sat, 'generate', lambda *_a, **_k: pytest.fail('publication must not solve again'))
    publication_id = h.publish(applied)['publication_id']
    published = h.get('coordinator', '/api/wishes')['wishes']
    assert next(row for row in published if row['id'] == lower['id'])['outcome_reasons'] == receipt['reasons']
    own = h.get('staff', '/api/wishes')['wishes']
    assert len(own) == 1 and own[0]['outcome'] == 'declined'
    assert own[0]['outcome_reasons'] == [{'day': '2026-10-04', 'kind': 'competing_wishes',
        'message': "A colleague's higher-priority preference was honoured."}]
    coordinator_snapshot = h.get('coordinator', '/api/publications/' + publication_id)['snapshot']
    assert next(row for row in coordinator_snapshot['wishes'] if row['id'] == lower['id'])['outcome_reasons'] == receipt['reasons']
    staff_snapshot = h.get('staff', '/api/publications/' + publication_id)['snapshot']
    assert len(staff_snapshot['wishes']) == 1
    assert staff_snapshot['wishes'][0]['outcome_reasons'] == own[0]['outcome_reasons']
    assert 'wish_worked' not in staff_snapshot
    import json
    assert 'person_name' not in json.dumps(staff_snapshot) and 'daily_weight' not in json.dumps(staff_snapshot)
    drill = h.get('staff', f'/api/fairness/sessions?person_id={person}&metric=wishes_declined&starts_on=2026-10-01&ends_on=2026-11-01')
    assert drill['publications'][0]['wishes'][0]['outcome_reasons'] == own[0]['outcome_reasons']
    h.get('staff', '/api/workspaces/' + applied['id'], status=403)
    h.get('staff', '/api/jobs/' + job['id'], status=403)


def test_greedy_receipt_uses_the_canonical_wish_outcomes(h):
    from packages.domain.generation import generate as greedy

    person = h.seed['people'][0]
    own = wish(h, '2026-10-04')['wish']
    workspace = h.shift(h.workspace(), people=[person])['workspace']
    data = h.snapshot(workspace)
    result = greedy(data, 5)
    receipt = next(row for row in result['declined_wishes'] if row['wish_id'] == own['id'])
    assert receipt['honoured'] == wish_outcomes(data)[own['id']][0] == 0
    assert receipt['reasons'][0]['kind'] == 'cover'


@pytest.mark.parametrize('kind,types,expected', [
    ('prefer_off', set(), 1), ('prefer_on', set(), 0),
    ('prefer_shift_type', set(), 1), ('prefer_shift_type', {'different'}, 0),
    ('avoid_shift_type', {'wanted'}, 0), ('avoid_shift_type', {'different'}, 1),
])
def test_wish_evaluator_preserves_all_kinds_and_local_start_day(kind, types, expected):
    data = {'organisation': {'timezone': 'Asia/Riyadh'},
            'workspace': {'starts_on': '2026-10-04', 'ends_on': '2026-10-05'},
            'shifts': [{'id': str(i), 'cancelled': False, 'assignment_type_id': typ,
                        'participants': [{'person_id': 'p', 'start': '2026-10-03T22:00:00Z'}]}
                       for i, typ in enumerate(types)],
            'wishes': [{'id': 'w', 'person_id': 'p', 'first_day': '2026-10-04', 'last_day': '2026-10-05',
                        'kind': kind, 'assignment_type_id': 'wanted'}]}
    assert wish_outcomes(data) == {'w': (expected, 1)}
    assert wish_evaluations(data)['w'][2] == ([] if expected else [date(2026, 10, 4)])


@pytest.fixture(scope='module')
def department_declined_wishes():
    from packages.domain import solver_summary, validation  # Load the lazy consumer before it can retain the stub.

    class CountedShifts(list):
        passes = 0

        def __iter__(self):
            self.passes += 1
            return super().__iter__()

    shifts, wishes, cover, evidence = CountedShifts(), [], [], []
    data = {'organisation': {'timezone': 'Asia/Riyadh'},
            'workspace': {'starts_on': '2026-10-01', 'ends_on': '2026-11-01'},
            'policies': [], 'people': {}, 'shifts': shifts, 'wishes': wishes}
    for person in range(150):
        pid = str(uuid.UUID(int=person + 1))
        data['people'][pid] = {'display_name': f'Synthetic {person}'}
        for offset in range(10):
            day = (date(2026, 10, 1) + timedelta(days=offset)).isoformat()
            wishes.append({'id': f'w-{person}-{day}', 'person_id': pid, 'kind': 'prefer_off',
                           'first_day': day, 'last_day': day, 'weight': 0.1})
    for offset in range(10):
        day = (date(2026, 10, 1) + timedelta(days=offset)).isoformat()
        for session in range(50):
            sid = f'{day}-{session}'
            shift = {'id': sid, 'cancelled': False, 'assignment_type_id': 'duty', 'type_snapshot': {'name': 'Clinic'},
                     'start': day + 'T08:00:00+03:00', 'end': day + 'T12:00:00+03:00',
                     'participants': [{'person_id': str(uuid.UUID(int=session * 3 + i + 1)), 'start': day + 'T08:00:00+03:00',
                                       'end': day + 'T12:00:00+03:00'} for i in range(3)]}
            shifts.append(shift)
            cover.append({'shift_id': sid, 'required': 3, 'scheduled': 3, 'available_eligible': 3,
                          'start': shift['start'], 'end': shift['end']})
            evidence.extend({'shift_id': sid, 'person_id': str(uuid.UUID(int=p + 1)), 'exclusions': []} for p in range(150))
    # These declines already have cover evidence: no validation trial or solver is needed.
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(validation, 'evaluate', lambda *_a, **_k: pytest.fail('unnecessary per-wish evaluation'))
        start = time.process_time()
        result = declined_wishes(data, {'findings': [], 'coverage': cover}, evidence)
        elapsed = time.process_time() - start
    return result, shifts.passes, elapsed


def test_150_people_500_sessions_1500_declines_have_bounded_work(department_declined_wishes):
    result, passes, _ = department_declined_wishes
    assert len(result) == 1500 and all(row['reasons'][0]['kind'] == 'cover' for row in result)
    assert passes <= 2


@pytest.mark.cpu_budget
def test_department_declined_wishes_cpu_budget(department_declined_wishes):
    elapsed = department_declined_wishes[-1]
    assert elapsed < 5, elapsed



def test_declined_receipt_contract_retains_every_kind_and_standing_occurrence_ids():
    from typing import get_args
    from pydantic import ValidationError
    from packages.contracts import schemas as s
    person = str(uuid.UUID(int=1))
    occurrence = f'standing:{person}:2026-10-05'
    assert get_args(s.WishKind) == s.WISH_KINDS
    for kind in s.WISH_KINDS:
        assert s.DeclinedWish(wish_id=occurrence, person_id=person, kind=kind, weight=.2,
                             honoured=0, reasons=[]).wish_id == occurrence
    assert s.CompetingWish(wish_id=occurrence, person_id=person, person_name='Synthetic',
                           kind='prefer_on', weight=.2, daily_weight=.2).wish_id == occurrence
    for invalid in ('standing:anything:2026-10-05', f'standing:{person}', 'arbitrary'):
        with pytest.raises(ValidationError):
            s.DeclinedWish(wish_id=invalid, person_id=person, kind='prefer_off', weight=.2, honoured=0, reasons=[])
    with pytest.raises(ValidationError):
        s.StaffWishInput(person_id=occurrence, kind='prefer_off', first_day='2026-10-05', last_day='2026-10-05')


@pytest.mark.parametrize('kind', ['prefer_on', 'prefer_shift_type'])
@pytest.mark.parametrize('winner_at_limit,expected', [(False, 'tradeoff'), (True, 'competing_wishes')])
def test_competitor_must_be_present_during_the_capacity_interval(h, kind, winner_at_limit, expected):
    person, other, colleague = h.seed['people'][:3]
    typ = h.configure('assignment_types', {'name': 'Split capacity evidence', 'starts_on': '2020-01-01',
        'definition': {'requirements': [
            {'from_minute': 0, 'to_minute': 120, 'minimum': 1, 'maximum': 4},
            {'from_minute': 120, 'to_minute': 210, 'minimum': 1, 'maximum': 2},
            {'from_minute': 210, 'to_minute': 240, 'minimum': 1, 'maximum': 4}]}})['record']['id']
    extra = {'assignment_type_id': typ} if kind == 'prefer_shift_type' else {}
    lower = wish(h, '2026-10-04', kind=kind, points=1, **extra)['wish']
    higher = wish(h, '2026-10-04', kind=kind, points=4, person=other, role='coordinator', **extra)['wish']
    workspace = h.shift(h.workspace(), people=[other, colleague], typ=typ)['workspace']
    if kind == 'prefer_shift_type':
        # A type wish is neutral on a day off; a different duty makes this one genuinely unmet.
        workspace = h.shift(workspace, start='2026-10-04T16:00:00+03:00',
                            end='2026-10-04T20:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(workspace)
    shift = data['shifts'][0]
    # A fourth eligible person keeps the tested interval full without reusing a participant ID.
    fourth = str(uuid.uuid4())
    data['people'][fourth] = {**copy.deepcopy(data['people'][colleague]), 'id': fourth,
                              'display_name': 'Synthetic split-cover colleague'}
    for name in ('memberships', 'grants', 'eligibilities', 'programme_memberships'):
        data.setdefault(name, []).extend({**copy.deepcopy(row), 'id': str(uuid.uuid4()), 'person_id': fourth}
                                         for row in list(data[name]) if row['person_id'] == colleague)
    source = next(p for p in shift['participants'] if p['person_id'] == colleague)
    clone = {**copy.deepcopy(source), 'id': str(uuid.uuid4()), 'person_id': fourth}
    shift['participants'].append(clone)
    gap_start, gap_end = '2026-10-04T10:00:00+03:00', '2026-10-04T11:30:00+03:00'
    gap = [{'start': shift['start'], 'end': gap_start}, {'start': gap_end, 'end': shift['end']}]
    incumbent = next(p for p in shift['participants'] if p['person_id'] == other)
    # Everyone reserves the whole duty; one person has an explicit off-cover gap in the tight interval.
    source['segments'] = gap if winner_at_limit else []
    clone['segments'] = []
    incumbent['segments'] = [] if winner_at_limit else gap
    original = copy.deepcopy(data)
    validation = evaluate(data)
    assert validation['valid'], validation['findings']
    # The new place exceeds maximum two only during 10-11:30; the honoured incumbent may be off cover then.
    receipt = next(row for row in declined_wishes(data, validation) if row['wish_id'] == lower['id'])
    reason = receipt['reasons'][0]
    assert reason['kind'] == expected
    if winner_at_limit:
        assert reason['competing_wishes'][0]['wish_id'] == higher['id']
    assert data == original


@pytest.mark.parametrize('kind', ['prefer_off', 'avoid_shift_type'])
@pytest.mark.parametrize('constraint,blocked_code', [
    ('capability', 'CAPABILITY_GAP'), ('supervision', 'SUPERVISION_GAP'),
])
def test_off_winner_is_not_attributed_when_it_cannot_replace_required_cover(h, monkeypatch, kind, constraint, blocked_code):
    from packages.domain import validation
    from packages.domain.candidate_evidence import candidate_evidence

    person, other, trainee = h.seed['people'][:3]
    capability = h.seed['capability']
    minimum = 1 if constraint == 'capability' else 2
    definition = {'requirements': [{'minimum': minimum, 'maximum': minimum}]}
    if constraint == 'capability':
        definition['requirements'][0]['capabilities'] = [{'capability_id': capability, 'minimum': 1}]
    else:
        definition['supervision'] = {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': capability, 'trainees_per_supervisor': 1, 'require_pairing': True}
    typ = h.configure('assignment_types', {'name': 'Required replacement evidence', 'starts_on': '2020-01-01',
                                          'definition': definition})['record']['id']
    extra = {'assignment_type_id': typ} if kind == 'avoid_shift_type' else {}
    lower = wish(h, '2026-10-04', kind=kind, points=1, **extra)['wish']
    wish(h, '2026-10-04', kind=kind, points=4, person=other, role='coordinator', **extra)
    first, last = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    participants = [{'person_id': person, 'start': first, 'end': last,
                     'participation': 'staff' if constraint == 'capability' else 'supervisor'}]
    if constraint == 'supervision':
        participants.append({'person_id': trainee, 'start': first, 'end': last,
                             'participation': 'trainee', 'supervisor_person_id': person})
    workspace = h.shift(h.workspace(), people=participants, typ=typ)['workspace']
    data = h.snapshot(workspace)
    # Everyone can take the clinic, but only A has the capability needed to replace A's contribution.
    data['grants'] = [g for g in data['grants'] if g['capability_id'] != capability or g['person_id'] == person]
    if constraint == 'supervision':
        for row in data['memberships']:
            if row['person_id'] == trainee:
                row['category_id'] = h.seed['trainee_category']
    original = copy.deepcopy(data)
    checked = evaluate(data)
    assert checked['valid'], checked['findings']
    shift = data['shifts'][0]
    candidate = candidate_evidence(data, other, shift)
    assert candidate['eligible_and_available'] and candidate['exclusions'] == []
    replacement = copy.deepcopy(data)
    next(p for p in replacement['shifts'][0]['participants'] if p['person_id'] == person)['person_id'] = other
    replacement_check = evaluate(replacement)
    assert not replacement_check['valid']
    assert blocked_code in {f['code'] for f in validation.finding_observations(replacement_check) if f['severity'] == 'error'}
    # Assigned wishes already have canonical cover evidence; no per-wish replacement evaluation is needed.
    monkeypatch.setattr(validation, 'evaluate', lambda *_a, **_k: pytest.fail('unnecessary off-winner placement trial'))
    receipt = next(row for row in declined_wishes(data, checked, [candidate]) if row['wish_id'] == lower['id'])
    reason = receipt['reasons'][0]
    assert reason['kind'] == 'cover' and reason['shift_id'] == shift['id']
    assert 'competing_wishes' not in reason
    assert data == original

@pytest.mark.parametrize('kind', ['prefer_on', 'prefer_shift_type'])
@pytest.mark.parametrize('constraint,blocked_code', [
    ('capability', 'CAPABILITY_GAP'), ('supervision', 'SUPERVISION_GAP'),
])
def test_on_winner_is_not_attributed_when_replacement_loses_required_cover(h, kind, constraint, blocked_code):
    from packages.domain import validation
    from packages.domain.candidate_evidence import candidate_evidence

    person, other, trainee = h.seed['people'][:3]
    capability = h.seed['capability']
    minimum = 1 if constraint == 'capability' else 2
    definition = {'requirements': [{'minimum': minimum, 'maximum': minimum}]}
    if constraint == 'capability':
        definition['requirements'][0]['capabilities'] = [{'capability_id': capability, 'minimum': 1}]
    else:
        definition['supervision'] = {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': capability, 'trainees_per_supervisor': 1, 'require_pairing': True}
    typ = h.configure('assignment_types', {'name': 'Required on-winner cover', 'starts_on': '2020-01-01',
                                          'definition': definition})['record']['id']
    extra = {'assignment_type_id': typ} if kind == 'prefer_shift_type' else {}
    lower = wish(h, '2026-10-04', kind=kind, points=1, **extra)['wish']
    higher = wish(h, '2026-10-04', kind=kind, points=4, person=other, role='coordinator', **extra)['wish']
    first, last = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    participants = [{'person_id': other, 'start': first, 'end': last,
                     'participation': 'staff' if constraint == 'capability' else 'supervisor'}]
    if constraint == 'supervision':
        h.configure('mentorships', {'person_id': trainee, 'mentor_id': other, 'strength': 'required',
            'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'assignment_type_ids': [typ]}, role='coordinator')
        participants.append({'person_id': trainee, 'start': first, 'end': last,
                             'participation': 'trainee'})
    workspace = h.shift(h.workspace(), people=participants, typ=typ)['workspace']
    if kind == 'prefer_shift_type':
        # A type wish is neutral on a day off; another duty makes this one genuinely unmet.
        workspace = h.shift(workspace, start='2026-10-04T16:00:00+03:00',
                            end='2026-10-04T20:00:00+03:00', people=[person])['workspace']
    data = h.snapshot(workspace)
    if constraint == 'supervision':
        assert next(p for p in data['shifts'][0]['participants'] if p['person_id'] == trainee)['supervisor_person_id'] == other
    # A is eligible for the clinic, but only incumbent B supplies its required capability or supervision.
    data['grants'] = [g for g in data['grants'] if g['capability_id'] != capability or g['person_id'] == other]
    if constraint == 'supervision':
        for row in data['memberships']:
            if row['person_id'] == trainee:
                row['category_id'] = h.seed['trainee_category']
    original = copy.deepcopy(data)
    checked = evaluate(data)
    assert checked['valid'], checked['findings']
    assert wish_outcomes(data)[lower['id']][0] == 0 and wish_outcomes(data)[higher['id']][0] == 1
    shift = data['shifts'][0]
    candidate = candidate_evidence(data, person, shift)
    assert candidate['eligible_and_available'] and candidate['exclusions'] == []
    replacement = copy.deepcopy(data)
    next(p for p in replacement['shifts'][0]['participants'] if p['person_id'] == other)['person_id'] = person
    replacement_check = evaluate(replacement)
    assert not replacement_check['valid']
    assert blocked_code in {f['code'] for f in validation.finding_observations(replacement_check) if f['severity'] == 'error'}
    receipt = next(row for row in declined_wishes(data, checked, [candidate]) if row['wish_id'] == lower['id'])
    reason = receipt['reasons'][0]
    assert reason['kind'] != 'competing_wishes' and 'competing_wishes' not in reason
    assert data == original


@pytest.mark.parametrize('barrier', ['locked', 'fixed', 'fill_gaps'])
def test_unchangeable_incumbent_is_not_attributed_to_wish_competition(h, barrier):
    person, other = h.seed['people'][:2]
    lower = wish(h, '2026-10-04', kind='prefer_on', points=1)['wish']
    wish(h, '2026-10-04', kind='prefer_on', points=4, person=other, role='coordinator')
    typ = h.configure('assignment_types', {'name': 'Immutable incumbent', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    data = h.snapshot(h.shift(h.workspace(), people=[other], typ=typ)['workspace'])
    if barrier == 'locked':
        data['shifts'][0]['locked'] = True
    elif barrier == 'fixed':
        data['shifts'][0]['participants'][0]['fixed'] = True
    else:
        data['generation_options'] = {'mode': 'fill_gaps'}
    original = copy.deepcopy(data)
    checked = evaluate(data)
    assert checked['valid'], checked['findings']
    receipt = next(row for row in declined_wishes(data, checked) if row['wish_id'] == lower['id'])
    assert receipt['reasons'][0]['kind'] == 'tradeoff'
    assert data == original


def test_feasibility_only_run_does_not_attribute_wish_priority(h, monkeypatch):
    from packages.domain import wish_reasons

    person, other = h.seed['people'][:2]
    wish(h, '2026-10-04', kind='prefer_on', points=1)
    wish(h, '2026-10-04', kind='prefer_on', points=4, person=other, role='coordinator')
    typ = h.configure('assignment_types', {'name': 'Feasibility preference evidence', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    data = h.snapshot(h.shift(h.workspace(), people=[], typ=typ)['workspace'])
    data['people'] = {pid: data['people'][pid] for pid in (person, other)}
    applied = []

    def receipt(*args, **kwargs):
        applied.append(kwargs.get('preferences_applied', True))
        return declined_wishes(*args, **kwargs)

    monkeypatch.setattr(wish_reasons, 'declined_wishes', receipt)
    result = generate(data, 10, feasibility_only=True)
    assert result['status'] == 'candidate_feasible'
    assert applied and not any(applied)
    assert result['declined_wishes']
    assert all(r['kind'] != 'competing_wishes' for row in result['declined_wishes'] for r in row['reasons'])
# Include this file in hosted supervision and team-lead qualification.
