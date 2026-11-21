"""Four-week departmental-sized candidate through the real shared validator."""
import copy
import pytest
from datetime import date, timedelta
import uuid
from packages.domain.generation import generate


def test_repair_reassigns_unpaired_trainee_with_valid_supervisor(h):
    trainee, supervisor = h.seed['people'][:2]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    h.policy({'kind': 'minimum_cover', 'minimum': 1}, {'person_id': trainee})
    typ = h.configure('assignment_types', {'name': 'Supervised FM clinic', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
            'require_pairing': True}}})['record']
    w = h.shift(h.workspace(), typ=typ['id'], people=[
        {'person_id': trainee, 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'participation': 'trainee'},
        {'person_id': supervisor, 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00', 'participation': 'supervisor'}])['workspace']
    data = h.snapshot(w)
    data['repair_conflicts'] = True
    data['generation_options'] = {'mode': 'rebalance'}
    result = generate(data)
    assert result['status'] == 'candidate_feasible'
    assert [r['person_id'] for r in result['removals']] == [trainee]
    assert result['additions'][0]['participant']['supervisor_person_id'] == supervisor
    data['shifts'][0]['locked'] = True
    kept = generate(data)
    assert kept['status'] == 'candidate_with_gaps' and not kept['removals']
    assert any(f['code'] == 'SUPERVISION_PAIRING' for f in kept['issues'])


def test_scoped_coverage_does_not_trial_people_outside_required_scope(h, monkeypatch):
    from packages.domain import generation
    required, preferred = h.seed['people'][:2]
    h.policy({'kind': 'minimum_cover', 'minimum': 1}, {'person_id': required})
    h.policy({'kind': 'allocation_preference', 'priority': 100}, {'person_id': preferred}, 'advisory')
    typ = h.configure('assignment_types', {'name': 'Scoped coverage clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    w = h.shift(h.workspace(), people=[], typ=typ['id'])['workspace']
    original = generation.evaluate
    def checked(data, **kwargs):
        assert all(p['person_id'] == required for s in data['shifts'] for p in s['participants'])
        return original(data, **kwargs)
    monkeypatch.setattr(generation, 'evaluate', checked)
    result = generate(h.snapshot(w))
    assert result['status'] == 'candidate_feasible'
    assert len(result['additions']) == 1


def test_quota_trials_only_use_matching_duties_dates_and_people(h, monkeypatch):
    from packages.domain import generation
    person = h.seed['people'][0]
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-06', 'ends_on': '2026-10-07',
              'metric': 'duties', 'comparison': 'target', 'amount': 1},
             {'person_id': person, 'service_ids': [h.seed['services'][1]]}, 'advisory')
    typ = h.configure('assignment_types', {'name': 'Optional clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    w = h.workspace()
    for day, service in (('04', 0), ('05', 1), ('06', 1)):
        w = h.shift(w, start=f'2026-10-{day}T08:00:00+03:00', end=f'2026-10-{day}T12:00:00+03:00',
                    people=[], service=service, typ=typ['id'])['workspace']
    original = generation.evaluate
    def checked(data, **kwargs):
        for shift in data['shifts']:
            for participant in shift['participants']:
                assert shift['start'].startswith('2026-10-06')
                assert participant['person_id'] == person
        return original(data, **kwargs)
    monkeypatch.setattr(generation, 'evaluate', checked)
    result = generate(h.snapshot(w))
    assert result['status'] == 'candidate_feasible'
    assert len(result['additions']) == 1


def test_shared_clock_boundaries_and_early_restriction_check(h, monkeypatch):
    from packages.domain import validation, generation
    person = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[])['workspace']
    h.policy({'kind': 'allocation_preference', 'priority': 0},
             {'start_time': '09:00', 'end_time': '11:00'}, severity='advisory')
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    data = h.snapshot(w)
    policy = next(p for p in data['policies'] if p['kind'] == 'allocation_preference')
    data['policies'] = [policy]
    expected = validation.evaluate(data)
    data['policies'] = [{**policy, 'id': str(uuid.uuid4())} for _ in range(100)]
    calls = []
    original_scope = validation.scope_intervals
    def counted_scope(d, scope, pid, a, b):
        if pid is None:
            calls.append(1)
        return original_scope(d, scope, pid, a, b)
    monkeypatch.setattr(validation, 'scope_intervals', counted_scope)
    assert validation.evaluate(data) == expected
    assert len(calls) == 1

    h.policy({'kind': 'restriction', 'weekdays': list(range(7)),
              'start_time': '00:00', 'end_time': '24:00'}, {'person_id': person})
    data = copy.deepcopy(h.snapshot(w))
    assert data['baseline']['snapshot_hash'] == w['baseline']['snapshot_hash']
    data['people'] = {person: data['people'][person]}
    restriction = next(p for p in data['policies'] if p['kind'] == 'restriction')
    trials = []
    original_evaluate = generation.evaluate
    def counted_evaluate(d, **kwargs):
        if any(s['participants'] for s in d['shifts']):
            trials.append(1)
        return original_evaluate(d, **kwargs)
    monkeypatch.setattr(generation, 'evaluate', counted_evaluate)
    assert generate(data)['additions'] == []
    assert not trials  # Mandatory restriction is rejected before a full-rota trial.
    restriction['definition']['severity'] = 'advisory'
    assert generate(data)['status'] == 'candidate_feasible'
    restriction['definition']['severity'] = 'mandatory'
    restriction['definition']['exception_allowed'] = True  # historical flag cannot waive safety
    data['exceptions'].append({'id': str(uuid.uuid4()), 'status': 'approved',
        'policy_id': restriction['id'], 'policy_revision': restriction['revision'],
        'person_id': person, 'starts_on': '2026-10-04', 'ends_on': '2026-10-05'})
    assert generate(data)['additions'] == []


def test_late_minimum_rest_binds_generation_without_baseline_refresh(h):
    person = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[person])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    baseline_hash = data['baseline']['snapshot_hash']
    data['people'] = {person: data['people'][person]}
    assert len(generate(data)['additions']) == 1
    h.policy({'kind': 'min_rest', 'hours': 24})
    data = h.snapshot(w)
    assert data['baseline']['snapshot_hash'] == baseline_hash
    data['people'] = {person: data['people'][person]}
    assert generate(data)['additions'] == []


def test_proposed_rules_do_not_consume_scope_work_and_unqualified_candidates_are_skipped(h, monkeypatch):
    from packages.domain import validation, generation
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    h.policy({'kind': 'min_rest', 'hours': 12})
    policy = h.snapshot(w)['policies'][-1]
    data['policies'] = [{**policy, 'id': str(uuid.uuid4()), 'status': 'proposed'} for _ in range(741)]
    original = copy.deepcopy(data)
    scope_calls = []
    original_scope = validation.scope_intervals
    monkeypatch.setattr(validation, 'scope_intervals', lambda *a, **k: (scope_calls.append(1), original_scope(*a, **k))[1])
    expected = validation.evaluate({**data, 'policies': []})
    assert validation.evaluate(data) == expected
    assert not scope_calls
    source = next(iter(data['people']))
    for i in range(142):
        pid = '000-ineligible-' + str(i)
        data['people'][pid] = {**data['people'][source], 'id': pid}
    # A qualified but absent candidate must also be rejected before a full
    # departmental evaluation, without weakening the final validation.
    absent = '000-absent'
    data['people'][absent] = {**data['people'][source], 'id': absent}
    for key in ('memberships', 'eligibilities', 'grants'):
        data[key].extend({**r, 'id': str(uuid.uuid4()), 'person_id': absent}
                        for r in list(data[key]) if r['person_id'] == source)
    data['availability'].append({'id': str(uuid.uuid4()), 'person_id': absent,
        'kind': 'recorded_leave', 'start': data['shifts'][0]['start'], 'end': data['shifts'][0]['end']})
    evaluations = []
    original_evaluate = generation.evaluate
    monkeypatch.setattr(generation, 'evaluate', lambda d, **kwargs: (evaluations.append(1), original_evaluate(d, **kwargs))[1])
    result = generate(data)
    assert result['status'] == 'candidate_feasible'
    assert len(evaluations) < 10
    assert not any(a['participant']['person_id'].startswith('000-ineligible-') for a in result['additions'])
    assert not any(a['participant']['person_id'] == absent for a in result['additions'])
    assert data['policies'] == original['policies']


def test_selected_generation_keeps_other_sessions_and_full_validation(h):
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    selected = data['shifts'][0]['id']
    data['generation_session_ids'] = [selected]
    result = generate(data)
    assert result['status'] == 'candidate_with_gaps'
    assert {a['shift_id'] for a in result['additions']} == {selected}
    assert any(f['shift_id'] != selected for f in result['unmet_requirements'])


def test_gap_first_redistribution_rebuilds_only_new_suggestions(h):
    preferred, alternative = h.seed['people'][:2]
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'day'})
    h.policy({'kind': 'allocation_preference', 'priority': 100}, {'person_id': preferred}, severity='advisory')
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-04T13:00:00+03:00', end='2026-10-04T17:00:00+03:00', people=[], service=1)['workspace']
    data = h.snapshot(w)
    data['people'] = {pid: p for pid, p in data['people'].items() if pid in {preferred, alternative}}
    data['eligibilities'] = [e for e in data['eligibilities'] if e['person_id'] == preferred or e['service_id'] != h.seed['services'][1]]
    original = copy.deepcopy(data)
    result = generate(data)
    assert result['status'] == 'candidate_feasible', result
    assert result['redistribution'] == {'attempted': True, 'improved': True, 'passes': 1}
    assert len(result['additions']) == 2
    assert data == original
    # A pre-existing assignment is never removed just to make the other session feasible.
    early = min(data['shifts'], key=lambda s: s['start'])
    early['participants'] = [{**result['additions'][0]['participant'], 'id': str(uuid.uuid4()),
        'person_id': preferred, 'shift_id': early['id'], 'start': early['start'], 'end': early['end']}]
    fixed = generate(data)
    assert fixed['status'] == 'candidate_with_gaps' and fixed['additions'] == []


def test_allocation_preferences_never_override_absence(h):
    preferred = h.seed['people'][0]
    h.policy({'kind': 'allocation_preference', 'priority': 100}, {'person_id': preferred}, severity='advisory')
    w = h.shift(h.workspace(), people=[])['workspace']
    assert generate(h.snapshot(w))['additions'][0]['participant']['person_id'] == preferred
    h.command('coordinator', 'availability.record', {'person_id': preferred, 'kind': 'recorded_leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00', 'private_reason': 'Confirmed leave'})
    result = generate(h.snapshot(w))
    assert result['status'] == 'candidate_feasible'
    assert all(r['participant']['person_id'] != preferred for r in result['additions'])


def test_session_preference_selects_later_clinic_but_never_bypasses_eligibility(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
        'metric': 'duties', 'comparison': 'target', 'amount': 1}, {'person_id': person}, 'advisory')
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 40},
        {'person_id': person, 'service_ids': [h.seed['services'][1]]}, 'advisory')
    duty = h.configure('assignment_types', {'name': 'Optional clinic pool', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    w = h.shift(h.workspace(), people=[], typ=duty['id'])['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
        people=[], service=1, typ=duty['id'])['workspace']
    data = h.snapshot(w)
    later = max(data['shifts'], key=lambda s: s['start'])
    result = generate(data)
    assert result['status'] == 'candidate_feasible'
    assert len(result['additions']) == 1
    assert result['additions'][0]['participant']['shift_id'] == later['id']
    data['eligibilities'] = [e for e in data['eligibilities']
        if e['person_id'] != person or e['service_id'] != h.seed['services'][1]]
    fallback = generate(data)
    assert fallback['status'] == 'candidate_feasible'
    assert len(fallback['additions']) == 1
    assert fallback['additions'][0]['participant']['shift_id'] != later['id']


def test_overtime_requires_explicit_scope_and_still_obeys_quota_and_rest(h):
    person = h.seed['people'][0]
    h.policy({'kind': 'max_hours', 'hours': 1, 'window': 'week', 'include_overtime': False})
    h.policy({'kind': 'min_rest', 'hours': 11})
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'maximum', 'amount': 1, 'overtime': 'only'}, {'person_id': person})
    w = h.shift(h.workspace(), people=[])['workspace']
    w = h.shift(w, start='2026-10-04T16:00:00+03:00', end='2026-10-04T20:00:00+03:00', people=[])['workspace']
    assert generate(h.snapshot(w))['additions'] == []
    h.policy({'kind': 'allocation_overtime'}, {'person_id': person}, severity='advisory')
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    data = copy.deepcopy(h.snapshot(w))
    result = generate(data)
    assert len(result['additions']) == 1 and result['additions'][0]['participant']['overtime'] is True
    assert result['additions'][0]['participant']['person_id'] == person
    assert result['status'] == 'candidate_with_gaps'
    # Raising the overtime quota still cannot erase minimum rest.
    next(p for p in data['policies'] if p['kind'] == 'block_quota')['definition']['rule']['amount'] = 3
    assert len(generate(data)['additions']) == 1
    # A permit covering only part of the duty cannot authorize the whole duty.
    next(p for p in data['policies'] if p['kind'] == 'allocation_overtime')['definition']['scope']['end_time'] = '09:00'
    assert generate(data)['additions'] == []


@pytest.mark.parametrize("engine", ["greedy", "cp_sat"])
def test_four_week_142_person_candidate_preserves_existing_work_and_respects_leave(h, engine):
    from packages.domain.cp_sat import generate as cp_generate
    allocate = generate if engine == "greedy" else cp_generate
    h.policy({'kind': 'min_rest', 'hours': 11})
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'day'})
    w = h.shift(h.workspace(start='2026-10-04', end='2026-11-01'), people=[])['workspace']
    data = h.snapshot(w)
    source = h.seed['people'][0]
    while len(data['people']) < 142:
        pid = str(uuid.uuid5(uuid.NAMESPACE_URL, 'department-fixture-' + str(len(data['people']))))
        data['people'][pid] = {**data['people'][source], 'id': pid}
        for key in ('memberships', 'eligibilities', 'grants'):
            data[key].extend({**r, 'id': str(uuid.uuid4()), 'person_id': pid} for r in list(data[key]) if r['person_id'] == source)
    template = data['shifts'][0]
    data['shifts'] = []
    for day in range(28):
        on = (date(2026, 10, 4) + timedelta(days=day)).isoformat()
        for start, end in [('08:00', '12:00'), ('16:00', '20:00')]:
            shift = copy.deepcopy(template)
            shift.update(id=str(uuid.uuid4()), start=on+'T'+start+':00+03:00', end=on+'T'+end+':00+03:00', participants=[])
            data['shifts'].append(shift)
    first = data['shifts'][0]
    first['locked'] = True
    first['participants'] = [{'id': str(uuid.uuid4()), 'person_id': source, 'shift_id': first['id'],
        'start': first['start'], 'end': first['end'], 'participation': 'staff', 'overtime': False, 'revision': 1}]
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    absent = pid
    data['availability'].append({'id': str(uuid.uuid4()), 'person_id': absent, 'kind': 'recorded_leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-11-01T00:00:00+03:00'})
    original = copy.deepcopy(data)
    result = allocate(data, time_budget_seconds=45)
    assert result['status'] == 'candidate_feasible', {k: v for k, v in result.items() if k != 'candidate_evidence'}
    assert len(result['additions']) == 55
    assert not any(r['shift_id'] == first['id'] for r in result['additions'])
    assert not any(r['participant']['person_id'] == absent for r in result['additions'])
    assert data == original


@pytest.mark.parametrize("engine", ["greedy", "cp_sat"])
def test_four_week_trainee_clinics_meet_targets_with_named_supervision(h, engine):
    from packages.domain.cp_sat import generate as cp_generate
    allocate = generate if engine == "greedy" else cp_generate
    trainee = h.seed['people'][0]
    h.command('coordinator', 'membership.create', {'person_id': trainee, 'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    h.policy({'kind': 'min_rest', 'hours': 11})
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'day'})
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-11-01',
        'metric': 'duties', 'comparison': 'minimum', 'amount': 28}, {'category_id': h.seed['trainee_category']})
    typ = h.configure('assignment_types', {'name': 'Four-week supervised clinics', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    w = h.shift(h.workspace(start='2026-10-04', end='2026-11-01'), people=[], typ=typ['id'])['workspace']
    data = h.snapshot(w)
    second = str(uuid.uuid4())
    data['people'][second] = {**data['people'][trainee], 'id': second}
    for key in ('memberships', 'eligibilities'):
        data[key].extend({**r, 'id': str(uuid.uuid4()), 'person_id': second} for r in list(data[key]) if r['person_id'] == trainee)
    data['grants'] = [r for r in data['grants'] if r['person_id'] != trainee]
    template = data['shifts'][0]
    data['shifts'] = []
    for day in range(28):
        on = (date(2026, 10, 4) + timedelta(days=day)).isoformat()
        for start, end in [('08:00', '12:00'), ('16:00', '20:00')]:
            shift = copy.deepcopy(template)
            shift.update(id=str(uuid.uuid4()), start=on+'T'+start+':00+03:00', end=on+'T'+end+':00+03:00')
            data['shifts'].append(shift)
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    result = allocate(data)
    assert result['status'] == 'candidate_feasible', result['validation']['findings'][:5]
    trainees = [r for r in result['additions'] if r['participant']['participation'] == 'trainee']
    assert len(trainees) == 56
    assert all(r['participant']['supervisor_person_id'] in h.seed['people'][1:] for r in trainees)
    assert all(r['actual'] == 28 for r in result['validation']['quota_report']['rows'])


@pytest.mark.parametrize("engine", ["greedy", "cp_sat"])
def test_four_week_mixed_rotations_allocate_separate_configured_requirements(h, engine):
    from packages.domain.cp_sat import generate as cp_generate
    allocate = generate if engine == "greedy" else cp_generate
    from .test_alpha_calendar import programme
    _, _, cohort = programme(h)
    person = h.seed['people'][0]
    types = [h.configure('rotation_types', {'name': name, 'starts_on': '2026-01-01'})['record']
             for name in ('UCC', 'Procedure-UCC')]
    for typ, first, last in ((types[0], '2026-10-01', '2026-10-15'), (types[1], '2026-10-15', '2026-10-29')):
        h.configure('rotations', {'person_id': person, 'cohort_id': cohort['id'], 'rotation_type_id': typ['id'],
            'starts_on': first, 'ends_on': last}, role='coordinator')
    for typ, service, amount in ((types[0], 0, 16), (types[1], 0, 8), (types[1], 1, 6)):
        h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-10-29',
            'metric': 'duties', 'comparison': 'target', 'amount': amount, 'prorate_department_days': True},
            {'rotation_type_id': typ['id'], 'service_ids': [h.seed['services'][service]]}, 'advisory')
    h.policy({'kind': 'min_rest', 'hours': 11})
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'day'})
    duty = h.configure('assignment_types', {'name': 'Optional clinic slots', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    w = h.shift(h.workspace(start='2026-10-01', end='2026-10-29'), people=[], typ=duty['id'])['workspace']
    data = h.snapshot(w)
    template = data['shifts'][0]
    data['shifts'] = []
    for day in range(28):
        on = (date(2026, 10, 1) + timedelta(days=day)).isoformat()
        for service in h.seed['services'][:2]:
            shift = copy.deepcopy(template)
            shift.update(id=str(uuid.uuid4()), start=on+'T08:00:00+03:00', end=on+'T12:00:00+03:00', service_ids=[service])
            data['shifts'].append(shift)
    data['focus_ids'] = [s['id'] for s in data['shifts']]
    before = copy.deepcopy(data)
    result = allocate(data)
    assert result['status'] == 'candidate_feasible', result.get('unmet_requirements')
    rows = result['validation']['quota_report']['rows']
    assert sorted((r['amount'], r['actual']) for r in rows) == [(3, 3), (4, 4), (8, 8)]
    assert len(result['additions']) == 15
    assert all(a['participant']['person_id'] == person for a in result['additions'])
    assert data == before


@pytest.mark.parametrize('relationship', ['any', 'mentor'])
def test_named_supervisor_preference_guides_pairing_without_granting_authority(h, relationship):
    trainee = h.seed['people'][0]
    supervisors = sorted(h.seed['people'][1:])
    preferred = supervisors[-1]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    if relationship == 'mentor':
        from .test_alpha_calendar import programme
        prog, _, _ = programme(h)
        h.configure('mentorships', {'person_id': trainee, 'mentor_id': preferred, 'programme_id': prog['id'],
            'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}, role='coordinator')
    h.policy({'kind': 'allocation_preference', 'priority': 10, 'supervisor_relationship': relationship,
        'supervisor_ids': [preferred] if relationship == 'any' else []},
        {'person_id': trainee}, severity='advisory')
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'target', 'amount': 1}, {'person_id': trainee}, severity='advisory')
    typ = h.configure('assignment_types', {'name': 'Named preferred supervision', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']
    start, end = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'
    w = h.shift(h.workspace(), typ=typ['id'], people=[{'person_id': pid, 'start': start, 'end': end,
        'participation': 'supervisor'} for pid in supervisors])['workspace']
    data = h.snapshot(w)
    result = generate(data)
    assert result['status'] == 'candidate_feasible'
    assignment = next(a['participant'] for a in result['additions'] if a['participant']['person_id'] == trainee)
    assert assignment['supervisor_person_id'] == preferred
    if relationship == 'mentor':
        from packages.domain.generation import allocation_settings
        expired = copy.deepcopy(data)
        expired['mentorships'][0]['ends_on'] = '2026-10-04'
        assert allocation_settings(expired, expired['shifts'][0], trainee, preferred)[0] == 0
        expired = copy.deepcopy(data)
        expired['programme_memberships'] = []
        assert allocation_settings(expired, expired['shifts'][0], trainee, preferred)[0] == 0
    data['grants'] = [g for g in data['grants'] if not (g['person_id'] == preferred and g['capability_id'] == h.seed['supervisor_capability'])]
    result = generate(data)
    assert result['status'] == 'candidate_feasible'
    assignment = next(a['participant'] for a in result['additions'] if a['participant']['person_id'] == trainee)
    assert assignment['supervisor_person_id'] != preferred


def test_repeat_supervisor_limit_blocks_manual_and_guides_allocation(h):
    from packages.domain.validation import evaluate
    trainee, first, second = h.seed['people'][:3]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week', 'group_by': 'supervisor'},
        {'person_id': trainee})
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'target', 'amount': 2}, {'person_id': trainee}, 'advisory')
    typ = h.configure('assignment_types', {'name': 'Supervisor variety', 'starts_on': '2020-01-01',
        'definition': {'supervision': {'trainee_category_ids': [h.seed['trainee_category']],
            'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 2}}})['record']
    w = h.workspace()
    for day in ('04', '05'):
        start, end = f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00'
        participants = [{'person_id': pid, 'start': start, 'end': end, 'participation': 'supervisor'}
                        for pid in (first, second)]
        participants.append({'person_id': trainee, 'start': start, 'end': end,
            'participation': 'trainee', 'supervisor_person_id': first})
        result = h.shift(w, start=start, end=end, typ=typ['id'], people=participants)
        w = result['workspace']
    assert any(f['code'] == 'MAX_DUTIES' and f['detail']['supervisor_person_id'] == first
               for f in result['validation']['findings'])
    h.lifecycle(w, 'submit', status=422)
    data = h.snapshot(w)
    later = max(data['shifts'], key=lambda s: s['start'])
    participant = next(p for p in later['participants'] if p['person_id'] == trainee)
    participant['supervisor_person_id'] = None
    assert 'SUPERVISOR_REQUIRED' in {f['code'] for f in evaluate(data)['findings']}
    later['participants'].remove(participant)
    generated = generate(data)
    assert generated['status'] == 'candidate_feasible', generated
    assert len(generated['additions']) == 1
    assert generated['additions'][0]['participant']['supervisor_person_id'] == second
    # The same outcome can be an advisory preference rather than a hard cap.
    balanced = copy.deepcopy(data)
    policy = next(p for p in balanced['policies'] if p['kind'] == 'max_duties')
    policy['kind'] = 'allocation_preference'
    policy['definition'].update(severity='advisory', exception_allowed=False,
        rule={'kind': 'allocation_preference', 'priority': 0, 'balance_by': 'supervisor', 'balance_weight': 2})
    result = generate(balanced)
    assert result['status'] == 'candidate_feasible'
    assert result['additions'][0]['participant']['supervisor_person_id'] == second
    historical = copy.deepcopy(data)
    earlier = min(historical['shifts'], key=lambda s: s['start'])
    historical['focus_ids'] = [later['id']]
    next(p for p in earlier['participants'] if p['person_id'] == trainee)['supervisor_person_id'] = None
    historical_later = next(s for s in historical['shifts'] if s['id'] == later['id'])
    historical_later['participants'].append({**participant, 'supervisor_person_id': second})
    assert 'SUPERVISOR_REQUIRED' in {f['code'] for f in evaluate(historical)['findings']}
    # The same supervisor is permitted again in the next configured calendar week.
    later['start'], later['end'] = '2026-10-11T08:00:00+03:00', '2026-10-11T12:00:00+03:00'
    later['participants'].append(participant)
    for p in later['participants']:
        p['start'], p['end'] = later['start'], later['end']
    participant['supervisor_person_id'] = first
    assert 'MAX_DUTIES' not in {f['code'] for f in evaluate(data)['findings']}


def test_clinic_balancing_uses_matching_duties_and_never_overrides_leave(h):
    first, second = h.seed['people'][:2]
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'balance_by': 'duties', 'balance_weight': 10},
        {'service_ids': [h.seed['services'][1]]}, 'advisory')
    w = h.workspace()
    for day, service, people in (('04', 1, [first]), ('05', 0, [second]), ('06', 0, [second]), ('07', 1, [])):
        w = h.shift(w, start=f'2026-10-{day}T08:00:00+03:00', end=f'2026-10-{day}T12:00:00+03:00',
            people=people, service=service)['workspace']
    data = h.snapshot(w)
    data['people'] = {pid:p for pid,p in data['people'].items() if pid in {first, second}}
    result = generate(data)
    assert result['status'] == 'candidate_feasible'
    assert len(result['additions']) == 1
    assert result['additions'][0]['participant']['person_id'] == second
    h.command('coordinator', 'availability.record', {'person_id': second, 'kind': 'recorded_leave',
        'start': '2026-10-07T00:00:00+03:00', 'end': '2026-10-08T00:00:00+03:00', 'private_reason': 'Synthetic leave'})
    data['availability'] = h.snapshot(w)['availability']
    assert generate(data)['additions'][0]['participant']['person_id'] == first
    from packages.domain.generation import allocation_settings
    later = max(data['shifts'], key=lambda s:s['start'])
    preference = next(p for p in data['policies'] if p['kind'] == 'allocation_preference')['definition']['rule']
    preference.update(balance_window='rolling', balance_window_days=3)
    assert allocation_settings(data, later, first)[0] == 0  # Oct 4 is outside Oct 5-7.
    preference['balance_window_days'] = 4
    assert allocation_settings(data, later, first)[0] == -10


def test_preference_scoring_stops_at_allocation_deadline(h, monkeypatch):
    from packages.domain import generation
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    clock, calls = [0.0], []
    original = generation.allocation_settings
    def slow_score(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(1)
        clock[0] += 2
        return result
    monkeypatch.setattr(generation.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(generation, 'allocation_settings', slow_score)
    result = generation._generate(data, 1)
    assert result['status'] == 'unknown_time_limit' and result['additions'] == []
    assert len(calls) == 1


def test_configured_mode_share_changes_allocation_and_counts_shared_duties_once(h):
    from packages.domain.generation import allocation_settings
    first, second = h.seed['people'][:2]
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'balance_by': 'service_mode',
        'balance_weight': 100, 'balance_target_mode': 'virtual', 'balance_target_share': 0.25,
        'balance_window': 'rota'}, {'service_ids': h.seed['services'][:2]}, 'advisory')
    w = h.workspace()
    for day, service, people in (('04', 0, [first]), ('05', 1, [first]), ('06', 1, [])):
        w = h.shift(w, start=f'2026-10-{day}T08:00:00+03:00', end=f'2026-10-{day}T12:00:00+03:00',
            people=people, service=service)['workspace']
    data = h.snapshot(w)
    data['people'] = {pid:p for pid,p in data['people'].items() if pid in {first, second}}
    for sid, mode in zip(h.seed['services'], ('physical', 'virtual')):
        data['services'][sid]['mode'] = mode
    assert generate(data)['additions'][0]['participant']['person_id'] == second
    rule = next(p for p in data['policies'] if p['kind'] == 'allocation_preference')['definition']['rule']
    rule['balance_target_share'] = 0.75
    assert generate(data)['additions'][0]['participant']['person_id'] == first
    old = min(data['shifts'], key=lambda s:s['start'])
    old['service_ids'] = h.seed['services'][:2]
    later = max(data['shifts'], key=lambda s:s['start'])
    rule['balance_target_share'] = 0.25
    assert allocation_settings(data, later, first)[0] == -50  # (one virtual + half shared)/two duties.
    data['services'][h.seed['services'][0]]['mode'] = 'unspecified'
    assert allocation_settings(data, later, first)[0] == -75  # Unspecified shared duty is not classified.


def test_daily_programme_targets_allocate_whole_sessions_and_keep_protected_time(h):
    person = h.seed['people'][0]
    programme = h.configure('programmes', {'name': 'Daily placement fixture', 'starts_on': '2026-01-01'})['record']
    h.configure('programme_memberships', {'person_id': person, 'programme_id': programme['id'],
        'starts_on': '2026-10-05', 'ends_on': '2026-10-09'}, role='coordinator')
    for weekdays, amount in [([6, 0, 1, 3], 2), ([2], 1)]:
        h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-11-01',
            'window_days': 1, 'metric': 'duties', 'comparison': 'target', 'amount': amount},
            {'programme_id': programme['id'], 'service_ids': [h.seed['services'][0]], 'weekdays': weekdays}, 'advisory')
    h.policy({'kind': 'restriction'}, {'programme_id': programme['id'], 'weekdays': [2],
        'start_time': '13:00', 'end_time': '17:00'})
    duty = h.configure('assignment_types', {'name': 'Optional whole clinic sessions', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    w = h.workspace()
    for day in range(4, 11):
        for hour in (8, 13):
            w = h.shift(w, start=f'2026-10-{day:02}T{hour:02}:00:00+03:00',
                end=f'2026-10-{day:02}T{hour+4:02}:00:00+03:00', people=[], typ=duty['id'])['workspace']
    data = h.snapshot(w)
    original = copy.deepcopy(data)
    result = generate(data)
    assert result['status'] == 'candidate_feasible', result['validation']['findings']
    assert len(result['additions']) == 7
    assert all(a['participant']['person_id'] == person for a in result['additions'])
    actual = {r['starts_on']: (r['amount'], r['actual']) for r in result['validation']['quota_report']['rows']}
    assert actual == {'2026-10-05': (2, 2), '2026-10-06': (2, 2), '2026-10-07': (1, 1), '2026-10-08': (2, 2)}
    shifts = {s['id']: s for s in data['shifts']}
    for addition in result['additions']:
        participant = addition['participant']
        shift = shifts[addition['shift_id']]
        assert participant['start'] == shift['start'] and participant['end'] == shift['end']
        assert not participant['start'].startswith('2026-10-07T13')
    assert data == original
    data['availability'].append({'id': str(uuid.uuid4()), 'person_id': person, 'kind': 'recorded_leave',
        'start': '2026-10-06T00:00:00+03:00', 'end': '2026-10-07T00:00:00+03:00'})
    unavailable = generate(data)
    assert unavailable['status'] == 'candidate_feasible'  # Advisory gaps remain visible without blocking safety-valid drafts.
    # leave covers this daily target window, so the target prorates to zero and the gap disappears.
    assert not any(f['code'] == 'QUOTA_BELOW_TARGET' for f in unavailable['unmet_requirements'])
    row = next(r for r in unavailable['validation']['quota_report']['rows'] if r['starts_on'] == '2026-10-06')
    assert (row['amount'], row['actual']) == (0, 0)
    assert len(unavailable['additions']) == 5
    assert all(not a['participant']['start'].startswith('2026-10-06') for a in unavailable['additions'])


def test_named_consultant_placement_adds_fellow_only_when_present_and_available(h):
    fellow, consultant = h.seed['people'][:2]
    programme = h.configure('programmes', {'name': 'Longitudinal placement', 'starts_on': '2026-01-01'})['record']
    h.configure('programme_memberships', {'person_id': fellow, 'programme_id': programme['id'],
        'starts_on': '2026-01-01', 'ends_on': '2027-01-01'}, role='coordinator')
    h.policy({'kind': 'minimum_cover', 'minimum': 1, 'when_person_ids': [consultant]},
        {'programme_id': programme['id'], 'person_id': fellow, 'service_id': h.seed['services'][0]}, 'advisory')
    duty = h.configure('assignment_types', {'name': 'Consultant placement fixture', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    first = h.shift(h.workspace(), people=[consultant], typ=duty['id'])
    w = h.shift(first['workspace'], start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00',
        people=[], typ=duty['id'])['workspace']
    data = h.snapshot(w)
    candidate = generate(data)
    assert len(candidate['additions']) == 1
    assert candidate['additions'][0]['participant']['person_id'] == fellow
    assert candidate['additions'][0]['participant']['start'].startswith('2026-10-04')
    assert not candidate['unmet_requirements']
    data['availability'].append({'id': str(uuid.uuid4()), 'person_id': fellow, 'kind': 'recorded_leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00'})
    absent = generate(data)
    assert absent['additions'] == []
    assert any(f['code'] == 'SCOPED_COVERAGE_GAP' for f in absent['unmet_requirements'])


def test_named_consultant_session_priority_selects_later_clinic(h):
    trainee, ordinary, preferred = h.seed['people'][:3]
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'metric': 'duties', 'comparison': 'target', 'amount': 1}, {'person_id': trainee}, 'advisory')
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 5,
        'supervisor_ids': [preferred]}, {'person_id': trainee}, 'advisory')
    duty = h.configure('assignment_types', {'name': 'Priority clinic fixture', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    w = h.workspace()
    for day, consultant in [('04', ordinary), ('05', preferred)]:
        start, end = f'2026-10-{day}T08:00:00+03:00', f'2026-10-{day}T12:00:00+03:00'
        w = h.shift(w, start=start, end=end, typ=duty['id'], people=[{'person_id': consultant,
            'start': start, 'end': end, 'participation': 'supervisor'}])['workspace']
    data = h.snapshot(w)
    result = generate(data)
    assert len(result['additions']) == 1
    assert result['additions'][0]['participant']['person_id'] == trainee
    assert result['additions'][0]['participant']['start'].startswith('2026-10-05')
    data['availability'].append({'id': str(uuid.uuid4()), 'person_id': trainee, 'kind': 'recorded_leave',
        'start': '2026-10-05T00:00:00+03:00', 'end': '2026-10-06T00:00:00+03:00'})
    fallback = generate(data)
    assert len(fallback['additions']) == 1
    assert fallback['additions'][0]['participant']['start'].startswith('2026-10-04')


def test_allocation_history_loads_published_work_outside_current_rota(h):
    from packages.domain.generation import allocation_settings
    person = h.seed['people'][0]
    h.policy({'kind': 'allocation_preference', 'priority': 10, 'balance_by': 'duties',
        'balance_window': 'rolling', 'balance_window_days': 7, 'balance_weight': 3}, {'person_id': person}, 'advisory')
    old = h.shift(h.workspace(start='2026-10-01', end='2026-10-02'),
        start='2026-10-01T08:00:00+03:00', end='2026-10-01T12:00:00+03:00')['workspace']
    h.publish(old)
    w = h.shift(h.workspace(), people=[])['workspace']
    snapshot = h.snapshot(w)
    current = next(s for s in snapshot['shifts'] if s['id'] in snapshot['focus_ids'])
    assert len(snapshot['shifts']) == 2
    assert allocation_settings(snapshot, current, person)[0] == 7
    # An unrelated unpublished draft must not inflate the shared history.
    draft = h.shift(h.workspace(start='2026-10-02', end='2026-10-03'),
        start='2026-10-02T08:00:00+03:00', end='2026-10-02T12:00:00+03:00')['workspace']
    assert draft['status'] == 'draft'
    assert allocation_settings(h.snapshot(w), current, person)[0] == 7
    expired = h.shift(h.workspace(start='2026-10-11', end='2026-10-12'),
        start='2026-10-11T08:00:00+03:00', end='2026-10-11T12:00:00+03:00', people=[])['workspace']
    later = h.snapshot(expired)
    current = next(s for s in later['shifts'] if s['id'] in later['focus_ids'])
    assert allocation_settings(later, current, person)[0] == 10


def test_grouped_supervisor_exposure_counts_either_selected_supervisor_once(h):
    from packages.domain.generation import allocation_settings
    from packages.contracts.schemas import AllocationPreference
    from pydantic import ValidationError
    trainee, director, deputy = h.seed['people'][:3]
    with pytest.raises(ValidationError):
        AllocationPreference(kind='allocation_preference', priority=0, balance_by='supervisor_group')
    h.policy({'kind': 'allocation_preference', 'priority': 10, 'balance_by': 'supervisor_group',
        'balance_weight': 3, 'balance_window': 'week', 'supervisor_ids': [director, deputy]}, {'person_id': trainee}, 'advisory')
    w = h.shift(h.workspace())['workspace']
    w = h.shift(w, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00', people=[])['workspace']
    data = h.snapshot(w)
    old, current = sorted(data['shifts'], key=lambda s: s['start'])
    current['participants'] = [{'person_id': pid, 'start': current['start'], 'end': current['end'],
                                'participation': 'supervisor', 'overtime': False} for pid in (director, deputy)]
    old['participants'][0]['supervisor_person_id'] = director
    # Working with the director also lowers priority for the deputy's next clinic.
    assert allocation_settings(data, current, trainee, deputy)[0] == 7
    assert allocation_settings(data, current, trainee, director)[0] == 7
    old['participants'].append(copy.deepcopy(old['participants'][0]))
    assert allocation_settings(data, current, trainee, deputy)[0] == 7
    old['participants'][0]['supervisor_person_id'] = deputy
    assert allocation_settings(data, current, trainee, director)[0] == 7
    for participant in old['participants']:
        participant['supervisor_person_id'] = trainee  # A different supervisor is outside the selected group.
    assert allocation_settings(data, current, trainee, deputy)[0] == 10
    assert allocation_settings(data, current, trainee, trainee)[0] == 0


@pytest.mark.parametrize('fixture_name', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_rolling_weekday_limit_blocks_repeat_and_guides_generation(request, fixture_name):
    h = request.getfixturevalue(fixture_name)
    from packages.domain.validation import evaluate
    person = h.seed['people'][0]
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'rolling', 'window_days': 8,
        'group_by': 'weekday'}, {'person_id': person})
    old = h.shift(h.workspace(start='2026-10-05', end='2026-10-06'),
        start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    h.publish(old)
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-12', 'ends_on': '2026-10-19',
        'metric': 'duties', 'comparison': 'target', 'amount': 1}, {'person_id': person}, 'advisory')
    duty = h.configure('assignment_types', {'name': 'Optional weekday clinic', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    w = h.workspace(start='2026-10-12', end='2026-10-19')
    for day in ('12', '13'):
        w = h.shift(w, start=f'2026-10-{day}T08:00:00+03:00', end=f'2026-10-{day}T12:00:00+03:00',
            people=[], typ=duty['id'])['workspace']
    result = generate(h.snapshot(w))
    assert len(result['additions']) == 1
    assert result['additions'][0]['participant']['start'].startswith('2026-10-13')
    bad = h.shift(h.workspace(start='2026-10-12', end='2026-10-13'),
        start='2026-10-12T08:00:00+03:00', end='2026-10-12T12:00:00+03:00')
    assert any(f['code'] == 'MAX_DUTIES' and f['detail']['weekday'] == 0 for f in bad['validation']['findings'])
    h.lifecycle(bad['workspace'], 'submit', status=422)
    later = h.shift(h.workspace(start='2026-10-19', end='2026-10-20'),
        start='2026-10-19T08:00:00+03:00', end='2026-10-19T12:00:00+03:00')['workspace']
    assert evaluate(h.snapshot(later))['valid']
    overnight = h.snapshot(bad['workspace'])
    previous = next(s for s in overnight['shifts'] if s['id'] not in overnight['focus_ids'])
    previous.update(start='2026-10-05T22:00:00+03:00', end='2026-10-06T02:00:00+03:00')
    previous['participants'][0].update(start=previous['start'], end=previous['end'])
    assert any(f['code'] == 'MAX_DUTIES' and f['detail']['weekday'] == 0 for f in evaluate(overnight)['findings'])
    current = next(s for s in overnight['shifts'] if s['id'] in overnight['focus_ids'])
    current.update(start='2026-10-13T08:00:00+03:00', end='2026-10-13T12:00:00+03:00')
    current['participants'][0].update(start=current['start'], end=current['end'])
    assert not any(f['code'] == 'MAX_DUTIES' for f in evaluate(overnight)['findings'])


@pytest.mark.parametrize('comparison', ['minimum', 'maximum', 'target'])
def test_first_placement_quota_counts_overlap_for_targets_and_history_for_bounds(h, comparison):
    experienced, unexposed = h.seed['people'][:2]
    p = h.configure('programmes', {'name': 'Exposure placement', 'starts_on': '2026-10-01', 'ends_on': '2026-12-01'})['record']
    for person in (experienced, unexposed):
        h.configure('programme_memberships', {'person_id': person, 'programme_id': p['id'],
            'starts_on': '2026-10-01', 'ends_on': '2026-12-01'}, role='coordinator')
    old = h.shift(h.workspace(start='2026-10-01', end='2026-10-02'),
        start='2026-10-01T08:00:00+03:00', end='2026-10-01T12:00:00+03:00')['workspace']
    h.publish(old)
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-01', 'ends_on': '2026-12-01',
        'metric': 'duties', 'comparison': comparison, 'amount': 1},
        {'programme_id': p['id'], 'service_id': h.seed['services'][0]},
        'mandatory' if comparison == 'maximum' else 'advisory')
    if comparison == 'maximum':
        h.policy({'kind': 'minimum_cover', 'minimum': 1}, {'programme_id': p['id']})
        h.policy({'kind': 'allocation_preference', 'priority': 100}, {'person_id': experienced}, 'advisory')
    duty = h.configure('assignment_types', {'name': 'Exposure opportunity', 'starts_on': '2020-01-01',
        'definition': {'requirements': []}})['record']
    w = h.shift(h.workspace(start='2026-11-15', end='2026-11-16'), typ=duty['id'], people=[],
        start='2026-11-15T08:00:00+03:00', end='2026-11-15T12:00:00+03:00')['workspace']
    data = h.snapshot(w)
    assert len(data['shifts']) == 2  # Historical publication is older than the ordinary 32-day context.
    result = generate(data)
    expected = {experienced, unexposed} if comparison == 'target' else {unexposed}
    assert len(result['additions']) == len(expected)
    assert {row['participant']['person_id'] for row in result['additions']} == expected
    rows = result['validation']['quota_report']['rows']
    assert {row['person_id'] for row in rows} == {experienced, unexposed}
    assert all(row['actual'] == row['amount'] == 1 and row['status'] == 'met' for row in rows)
    if comparison in {'minimum', 'target'}:
        assert all(row['proration']['working_days'] == 1 for row in rows)
    else:
        assert all('proration' not in row for row in rows)
    assert result['validation']['valid']


def test_last_resort_overtime_priority_preserves_regular_order_and_safety(h):
    first, second = h.seed['people'][:2]
    h.policy({'kind': 'allocation_preference', 'priority': 100, 'overtime_priority': -1},
        {'person_id': first}, 'advisory')
    h.policy({'kind': 'allocation_overtime'}, severity='advisory')
    w = h.shift(h.workspace(), people=[])['workspace']
    data = copy.deepcopy(h.snapshot(w))
    # Give both candidates the same clinical authority in this isolated fixture.
    data['people'] = {p: data['people'][p] for p in (first, second)}
    for key in ('memberships', 'eligibilities', 'grants'):
        data[key] = [r for r in data[key] if r['person_id'] != second]
        data[key].extend({**r, 'id': str(uuid.uuid4()), 'person_id': second}
                        for r in list(data[key]) if r['person_id'] == first)
    assert generate(data)['additions'][0]['participant']['person_id'] == first
    h.policy({'kind': 'max_hours', 'hours': 1, 'window': 'week', 'include_overtime': False})
    w = h.refresh_draft_baseline(h.detail(w)['workspace'])
    data['policies'] = h.snapshot(w)['policies']
    result = generate(data)
    assert result['additions'][0]['participant']['person_id'] == second
    assert result['additions'][0]['participant']['overtime'] is True
    data['availability'].append({'id': str(uuid.uuid4()), 'person_id': second, 'kind': 'recorded_leave',
        'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00'})
    assert generate(data)['additions'][0]['participant']['person_id'] == first
    data['eligibilities'] = []
    assert generate(data)['additions'] == []
