import copy
import pytest

from packages.domain.candidate_evidence import candidate_evidence
from packages.domain.time import aware
from packages.domain.validation import qualified, restriction_blocks, rotation_blocks


def test_candidate_evidence_uses_canonical_decisions_and_omits_private_data(h):
    w = h.shift(h.workspace())['workspace']
    data = h.snapshot(w)
    shift = data['shifts'][0]
    pid = h.seed['people'][0]
    a, b = aware(shift['start']), aware(shift['end'])
    assert candidate_evidence(data, pid, shift)['eligible_and_available']
    missing = copy.deepcopy(data)
    for row in missing['eligibilities']:
        if row['person_id'] == pid:
            row['starts_on'] = '2027-01-01'
    evidence = candidate_evidence(missing, pid, shift)
    assert not qualified(missing, pid, shift, shift['type_snapshot']['definition'], a, b)
    assert evidence['exclusions'][0]['code'] == 'ELIGIBILITY_MISSING'
    assert evidence['exclusions'][0]['existing_record_ids']
    missing['availability'].append({'id': 'absence', 'person_id': pid,
        'start': shift['start'], 'end': shift['end'], 'private_reason': 'secret diagnosis'})
    missing['rotations'].append({'id': 'rotation', 'person_id': pid,
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01', 'department_available': False})
    evidence = candidate_evidence(missing, pid, shift)
    assert {r['code'] for r in evidence['exclusions']} == {
        'ELIGIBILITY_MISSING', 'UNAVAILABLE', 'EXTERNAL_ROTATION'}
    assert rotation_blocks(missing, pid, a, b)
    assert 'secret diagnosis' not in str(evidence)
    h.policy({'kind': 'restriction', 'weekdays': list(range(7)),
              'start_time': '00:00', 'end_time': '24:00'})
    restricted = h.snapshot(w)
    assert restriction_blocks(restricted, pid, shift, a, b)
    assert any(r['code'] == 'RESTRICTION' for r in candidate_evidence(restricted, pid, shift)['exclusions'])


def test_missing_assignment_type_fails_closed_without_keyerror():
    from tests.test_schedule_score import data, shift

    session = shift('missing-type', 5, [])
    snapshot = data([session])
    snapshot['policies'] = []
    snapshot['types'].pop(session['assignment_type_id'])

    evidence = candidate_evidence(snapshot, 'a', session)

    assert not evidence['eligible_and_available']
    assert evidence['exclusions'][0]['code'] == 'ELIGIBILITY_MISSING'


@pytest.mark.parametrize('feasibility_only', [False, True])
@pytest.mark.parametrize('protection,reason', [
    ('fixed', 'ASSIGNMENT_FIXED'), ('locked', 'SESSION_LOCKED'),
    ('unselected', 'OUTSIDE_EDITABLE_SCOPE'), ('published', 'PUBLISHED_ASSIGNMENT'),
])
def test_solver_records_focused_choices_and_explains_fixed_work(h, protection, reason, feasibility_only):
    from packages.domain.cp_sat import generate

    data = h.snapshot(h.shift(h.workspace())['workspace'])
    shift = data['shifts'][0]
    participant = shift['participants'][0]
    participant['fixed'] = protection == 'fixed'
    if protection == 'locked':
        shift['locked'] = True
    elif protection == 'unselected':
        data['generation_session_ids'] = []
    elif protection == 'published':
        data['workspace']['status'] = 'published'
        shift['locked'] = True
    before = copy.deepcopy(data)
    result = generate(data, 5, workers=1, feasibility_only=feasibility_only)
    assert result['status'] == 'candidate_feasible', result
    rows = result['candidate_evidence']
    expected = set(data['people']) if feasibility_only or protection == 'fixed' else {participant['person_id']}
    assert len(rows) == len(expected)
    assert {(row['shift_id'], row['person_id']) for row in rows} == {
        (shift['id'], pid) for pid in expected}
    fixed = next(row for row in rows if row['person_id'] == participant['person_id'])
    assert fixed['fixed'] and fixed['fixed_reason'] == reason
    canonical = candidate_evidence(data, participant['person_id'], shift)
    assert {key: fixed[key] for key in canonical} == canonical
    assert all(row['person_id'] != participant['person_id'] for row in result.get('removals', []))
    assert data == before


@pytest.mark.parametrize('feasibility_only', [False, True])
@pytest.mark.parametrize('population', ['generation_scope', 'planning_population'])
def test_scoped_evidence_excludes_fixed_context_only_for_generation(h, population, feasibility_only):
    from packages.domain.cp_sat import generate
    from .test_generation_population import scope

    data = h.snapshot(h.shift(h.workspace())['workspace'])
    shift = data['shifts'][0]
    outsider = shift['participants'][0]
    outsider['fixed'] = True
    selected = next(pid for pid in data['people'] if pid != outsider['person_id'])
    data[population] = (scope('person_ids', selected) if population == 'generation_scope'
                        else {shift['id']: [selected]})
    data['generation_options'] = {'mode': 'rebalance'}
    before = copy.deepcopy(data)

    result = generate(data, 5, workers=1, feasibility_only=feasibility_only)

    assert result['status'] == 'candidate_feasible', result
    assert {row['person_id'] for row in result['candidate_evidence']} == (
        set(data['people']) if feasibility_only else {selected})
    assert not result['removals']
    assert all(row['participant']['person_id'] == selected for row in result['additions'])
    if feasibility_only:
        fixed = next(row for row in result['candidate_evidence'] if row['person_id'] == outsider['person_id'])
        assert fixed['fixed'] and fixed['fixed_reason'] == 'ASSIGNMENT_FIXED'
    assert data == before


def test_focused_evidence_audience_excludes_inactive_and_neighbour_sessions(h):
    from packages.domain.candidate_evidence import focused_candidate_people

    data = h.snapshot(h.shift(h.workspace())['workspace'])
    shift = data['shifts'][0]
    assert set(focused_candidate_people(data, shift)) == set(data['people'])
    assert not focused_candidate_people(data, {**shift, 'cancelled': True})
    assert not focused_candidate_people(data, {**shift, 'id': 'published-neighbour'})


def test_fixed_evidence_keeps_mandatory_exclusions_and_recomputes_choice_metadata(h):
    from packages.domain.cp_sat import generate

    w = h.shift(h.workspace())['workspace']
    h.policy({'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'})
    data = h.snapshot(w)
    shift = data['shifts'][0]
    participant = shift['participants'][0]
    participant['fixed'] = True
    result = generate(data, 5, workers=1, feasibility_only=True)
    assert any(issue['code'] == 'RESTRICTION' and issue['person_id'] == participant['person_id']
               for issue in result.get('issues', [])), result
    fixed = next(row for row in result['candidate_evidence'] if row['person_id'] == participant['person_id'])
    assert fixed['fixed'] and not fixed['eligible_and_available']
    assert 'RESTRICTION' in {reason['code'] for reason in fixed['exclusions']}
    participant['fixed'] = False
    fresh = generate(data, 5, workers=1, feasibility_only=True, _candidate_evidence=result['candidate_evidence'])
    row = next(row for row in fresh['candidate_evidence'] if row['person_id'] == participant['person_id'])
    assert not row.get('fixed') and 'fixed_reason' not in row
    assert not row['eligible_and_available'] and row['exclusions'] == fixed['exclusions']


@pytest.mark.parametrize('indexed', [False, True])
def test_fixed_manual_overtime_still_supplies_cover_with_recorded_evidence(h, indexed):
    from packages.domain.candidate_evidence import direct_coverage_shortages

    data = h.snapshot(h.shift(h.workspace())['workspace'])
    shift = data['shifts'][0]
    shift['type_snapshot']['definition']['extra_cover'] = True
    shift['type_snapshot']['definition']['extra_cover_unfilled'] = 'gap'
    data['people'] = {pid: row for pid, row in data['people'].items()
                      if pid == shift['participants'][0]['person_id']}
    row = candidate_evidence(data, shift['participants'][0]['person_id'], shift)
    assert {reason['code'] for reason in row['exclusions']} == {'OVERTIME_NOT_ALLOWED'}
    assert direct_coverage_shortages(data, [row] if indexed else [], {shift['id']}) == []
    shift['participants'] = []
    [shortage] = direct_coverage_shortages(data, [row] if indexed else [], {shift['id']})
    assert shortage['available_upper_bound'] == 0


def test_scope_cache_is_snapshot_local_and_preserves_person_windows(h, monkeypatch):
    from packages.domain import validation
    w = h.shift(h.workspace())['workspace']
    data = h.snapshot(w)
    a, b = aware(data['shifts'][0]['start']), aware(data['shifts'][0]['end'])
    first, second = h.seed['people'][:2]
    data['programmes'] = {'programme': {'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}}
    data['programme_memberships'] = [{'person_id': first, 'programme_id': 'programme',
        'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}]
    cached = {**data, '_scope_interval_cache': {}}
    original = validation._scope_intervals
    calls = []
    def counted(*args):
        calls.append(1)
        return original(*args)
    monkeypatch.setattr(validation, '_scope_intervals', counted)
    assert validation.scope_intervals(cached, {}, first, a, b) == [(a, b)]
    assert validation.scope_intervals(cached, {}, second, a, b) == [(a, b)]
    assert len(calls) == 1
    scope = {'programme_id': 'programme'}
    assert validation.scope_intervals(cached, scope, first, a, b) == [(a, b)]
    assert validation.scope_intervals(cached, scope, second, a, b) == []
    validation.scope_intervals(cached, scope, first, a, b).clear()
    assert validation.scope_intervals(cached, scope, first, a, b) == [(a, b)]
    fresh = {**data, 'programme_memberships': [], '_scope_interval_cache': {}}
    assert validation.scope_intervals(fresh, scope, first, a, b) == []
    assert '_scope_interval_cache' not in data


@pytest.mark.cpu_budget
def test_shortage_proof_counts_each_person_once_and_cites_source_records(h):
    from packages.domain.cp_sat import generate
    person = h.seed['people'][0]
    h.policy({'kind': 'restriction', 'weekdays': list(range(7)), 'start_time': '00:00', 'end_time': '24:00'})
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    data['people'] = {person: data['people'][person]}
    shift = data['shifts'][0]
    data['availability'].append({'id': 'approved-absence', 'person_id': person, 'kind': 'approved_leave',
        'start': shift['start'], 'end': shift['end'], 'private_reason': 'private diagnosis'})
    result = generate(data, 5)
    assert result['status'] == 'requirements_conflict'
    proof = result['diagnostics'][0]
    assert proof['proof_kind'] == 'necessary_condition' and proof['proven']
    assert (proof['required'], proof['available_upper_bound']) == (1, 0)
    assert len(proof['candidates']) == 1
    assert {r['code'] for r in proof['candidates'][0]['exclusions']} == {'UNAVAILABLE', 'RESTRICTION'}
    assert proof['candidates'][0]['exclusions'][0]['record_id'] == 'approved-absence'
    assert proof['input_fingerprint'] == data['fingerprint']
    # The necessary-condition proof is produced before a complete diagnostic
    # search; a tiny recorded search phase may still include solver setup.
    assert 0 <= result['phase_timings']['search_seconds'] < 1
    assert result['diagnostics_complete'] is False
    assert 'private diagnosis' not in str(result)
    assert proof['message'].startswith('Needs 1 more person for this interval — nobody eligible is free')
    assert result['summary']['top_reasons'][0]['message'] == proof['message']
    assert proof['message'] in result['summary']['open_places'][0]['message']


def test_capability_shortage_uses_distinct_people_and_dated_grants(h):
    from packages.domain.candidate_evidence import direct_coverage_shortages
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    people = h.seed['people'][:2]
    data['people'] = {pid: data['people'][pid] for pid in people}
    shift = data['shifts'][0]
    shift.update(start='2026-10-04T22:00:00+03:00', end='2026-10-05T02:00:00+03:00')
    capability = h.seed['capability']
    shift['type_snapshot']['definition']['requirements'][0]['capabilities'] = [{'capability_id': capability, 'minimum': 1}]
    template = next(g for g in data['grants'] if g['capability_id'] == capability)
    data['grants'] = [{**template, 'id': 'first-night-grant', 'person_id': people[0], 'ends_on': '2026-10-05'},
                      {**template, 'id': 'second-night-grant', 'person_id': people[1], 'starts_on': '2026-10-05'}]
    # Different people provide the capability on either side of midnight.
    assert direct_coverage_shortages(data, [], {shift['id']}) == []
    data['grants'][1]['starts_on'] = '2026-10-06'
    proof, = direct_coverage_shortages(data, [], {shift['id']})
    assert proof['code'] == 'INSUFFICIENT_CAPABILITY_CANDIDATES'
    assert aware(proof['start']) == aware('2026-10-05T00:00:00+03:00')
    assert proof['required'] == 1 and proof['available_upper_bound'] == 0
    assert len(proof['candidates']) == 2
    assert proof['capability_id'] == capability
    assert {reason['existing_record_ids'][0] for person in proof['candidates'] for reason in person['exclusions']
            if reason['code'] == 'CAPABILITY_NOT_GRANTED'} == {'first-night-grant', 'second-night-grant'}
    name = data['capabilities'][capability]['name']
    assert proof['message'] == (f'Needs 1 more person with {name} for this interval — nobody eligible is free'
                                f' (main blocker: missing {name})')


def test_all_trainee_pool_cannot_provide_its_own_supervision(h):
    from packages.domain.candidate_evidence import direct_coverage_shortages
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    shift = data['shifts'][0]
    person = h.seed['people'][0]
    category = next(r['category_id'] for r in data['memberships'] if r['person_id'] == person)
    data['people'] = {person: data['people'][person]}
    shift['type_snapshot']['definition']['supervision'] = {
        'trainee_category_ids': [category], 'capability_id': h.seed['capability'],
        'trainees_per_supervisor': 2, 'require_pairing': True}
    proof, = direct_coverage_shortages(data, [], {shift['id']})
    assert proof['available_upper_bound'] == 0
    assert proof['candidates'][0]['exclusions'][0]['code'] == 'SUPERVISION_UNAVAILABLE'
    assert proof['message'].endswith('(main blocker: no eligible supervisor)')
    shift['type_snapshot']['definition']['supervision']['trainee_category_ids'] = []
    assert direct_coverage_shortages(data, [], {shift['id']}) == []


@pytest.mark.parametrize('required, available, message', [
    (1, 0, 'Needs 1 more person for this interval — nobody eligible is free'),
    (2, 0, 'Needs 2 more people for this interval — nobody eligible is free'),
    (2, 1, 'Needs at least 1 more person for this interval — at most 1 eligible person could work it'),
    (3, 2, 'Needs at least 1 more person for this interval — at most 2 eligible people could work it'),
])
def test_shortage_wording_counts_missing_people_and_available_pool(h, required, available, message):
    from packages.domain.candidate_evidence import direct_coverage_shortages
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    shift = data['shifts'][0]
    data['people'] = dict(list(data['people'].items())[:available])
    shift['type_snapshot']['definition']['requirements'][0]['minimum'] = required
    proof, = direct_coverage_shortages(data, [], {shift['id']})
    assert proof['message'] == message
    assert (proof['required'], proof['available_upper_bound']) == (required, available)


def test_shortage_wording_does_not_claim_a_competing_duty_candidate_is_free(h):
    from packages.domain.candidate_evidence import direct_coverage_shortages
    pid = h.seed['people'][0]
    workspace = h.shift(h.workspace(), people=[pid])['workspace']
    workspace = h.shift(workspace, people=[])['workspace']
    data = h.snapshot(workspace)
    data['people'] = {pid: data['people'][pid]}
    shift = next(s for s in data['shifts'] if not s['participants'])
    shift['type_snapshot']['definition']['requirements'][0]['minimum'] = 2
    data['focus_ids'] = [shift['id']]
    proof, = direct_coverage_shortages(data, [], {shift['id']})
    assert proof['available_upper_bound'] == 1
    assert proof['message'] == ('Needs at least 1 more person for this interval — '
                                'at most 1 eligible person could work it')
    assert 'competing duties' in proof['boundary']


@pytest.mark.parametrize('code, blocker', [
    ('UNAVAILABLE', 'unavailability'), ('EXTERNAL_ROTATION', 'rotation away from the department'),
    ('ELIGIBILITY_MISSING', 'clinic eligibility'), ('MEMBERSHIP_MISSING', 'department membership'),
    ('RANK_NOT_ALLOWED', 'rank eligibility'), ('PERSON_INACTIVE', 'inactive staff'),
    ('RESTRICTION', 'Protected teaching'), ('DUTY_NOT_EDITABLE', 'session outside this run'),
])
def test_shortage_wording_names_only_recorded_blockers(h, code, blocker):
    from packages.domain.candidate_evidence import direct_coverage_shortages
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    shift = data['shifts'][0]
    pid = next(iter(data['people']))
    data['people'] = {pid: data['people'][pid]}
    data['policies'] = [{'id': 'restriction', 'name': 'Protected teaching'}]
    evidence = [{'person_id': pid, 'shift_id': shift['id'],
                 'exclusions': [{'code': code, 'rule_id': 'restriction', 'private_reason': 'private diagnosis'}]}]
    proof, = direct_coverage_shortages(data, evidence, {shift['id']})
    assert proof['message'] == ('Needs 1 more person for this interval — nobody eligible is free'
                                f' (main blocker: {blocker})')
    assert 'private diagnosis' not in proof['message']


def test_shortage_main_blocker_counts_people_once_and_breaks_ties_consistently(h):
    from packages.domain.candidate_evidence import direct_coverage_shortages
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    shift = data['shifts'][0]
    data['people'] = dict(list(data['people'].items())[:2])
    first, second = data['people']
    evidence = [{'person_id': first, 'shift_id': shift['id'], 'exclusions': [
        {'code': 'UNAVAILABLE'}, {'code': 'EXTERNAL_ROTATION'}, {'code': 'EXTERNAL_ROTATION'}]},
        {'person_id': second, 'shift_id': shift['id'], 'exclusions': [{'code': 'UNAVAILABLE'}]}]
    proof, = direct_coverage_shortages(data, evidence, {shift['id']})
    assert proof['message'].endswith('(main blocker: unavailability)')
    evidence[1]['exclusions'].append({'code': 'EXTERNAL_ROTATION'})
    proof, = direct_coverage_shortages(data, evidence, {shift['id']})
    assert proof['message'].endswith('(main blocker: rotation away from the department)')
    for row in evidence:
        row['exclusions'].reverse()
    again, = direct_coverage_shortages(data, list(reversed(evidence)), {shift['id']})
    assert again['message'] == proof['message']


@pytest.mark.parametrize('required', [1, 2])
def test_locked_shortage_wording_is_grammatical_and_consistent_with_summary(h, required):
    from packages.domain.candidate_evidence import direct_coverage_shortages
    from packages.domain.solver_summary import summarise
    from packages.domain.validation import evaluate
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    shift = data['shifts'][0]
    shift['locked'] = True
    shift['type_snapshot']['definition']['requirements'][0]['minimum'] = required
    proof, = direct_coverage_shortages(data, [], set())
    expected = (f"This session is locked with 0 of {required} {'person' if required == 1 else 'people'}. "
                'Unlock it or add someone by hand.')
    summary = summarise(data, evaluate(data), [], set(), diagnostics=[proof])
    assert proof['message'] == summary['open_places'][0]['message'] == expected
    assert summary['top_reasons'][0]['message'] == expected


@pytest.mark.parametrize('protection', ['keep_existing','rebalance_selected'])
def test_selected_shortage_excludes_new_outsiders_but_keeps_retained_context(h, protection):
    from packages.domain.candidate_evidence import direct_coverage_shortages
    from .test_generation_population import scope
    w = h.shift(h.workspace(), people=[])['workspace']
    data = h.snapshot(w)
    shift = data['shifts'][0]
    selected, outsider = h.seed['people'][:2]
    data['people'] = {pid: data['people'][pid] for pid in (selected,outsider)}
    data['generation_scope'] = scope('person_ids', selected, protection=protection)
    data['eligibilities'] = [r for r in data['eligibilities'] if r['person_id'] != selected]
    evidence = [candidate_evidence(data, selected, shift)]  # CP's selected-only audience
    [proof] = direct_coverage_shortages(data, evidence, {shift['id']})
    assert proof['available_upper_bound'] == 0 and proof['proof_kind'] == 'necessary_condition'
    assert {r['code'] for c in proof['candidates'] if c['person_id'] == outsider for r in c['exclusions']} == {'OUTSIDE_GENERATION_POPULATION'}
    # Retained eligible outsiders remain valid cover and supervision context.
    shift['participants'] = [{'person_id': outsider, 'start': shift['start'], 'end': shift['end'], 'participation': 'staff'}]
    assert direct_coverage_shortages(data, evidence, {shift['id']}) == []


@pytest.mark.parametrize('retained', [False, True])
def test_selected_trainee_upper_bound_retains_outsider_supervision_context(h, retained):
    from packages.domain.candidate_evidence import direct_coverage_shortages
    from .test_generation_population import scope
    data = h.snapshot(h.shift(h.workspace(), people=[])['workspace'])
    selected, supervisor = h.seed['people'][:2]
    data['people'] = {pid: data['people'][pid] for pid in (selected,supervisor)}
    for row in data['memberships']:
        if row['person_id'] == selected:
            row['category_id'] = h.seed['trainee_category']
    data['generation_scope'] = scope('person_ids', selected)
    shift = data['shifts'][0]
    shift['type_snapshot']['definition']['requirements'][0]['minimum'] = 2
    shift['type_snapshot']['definition']['supervision'] = {
        'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
        'trainees_per_supervisor': 1, 'require_pairing': True}
    if retained:
        shift['participants'] = [{'person_id': supervisor, 'start': shift['start'], 'end': shift['end'], 'participation': 'staff'}]
    proof = direct_coverage_shortages(data, [candidate_evidence(data, selected, shift)], {shift['id']})
    if retained:
        assert proof == []
    else:
        [diagnostic] = proof
        assert diagnostic['available_upper_bound'] == 0
        assert any(r['code'] == 'SUPERVISION_UNAVAILABLE' for c in diagnostic['candidates'] for r in c['exclusions'])
# Include this file in hosted supervision and team-lead qualification.
