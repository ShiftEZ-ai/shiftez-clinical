"""Preference objective parity, including immutable work and conditional person denominators."""
from collections import defaultdict

import pytest
from ortools.sat.python import cp_model
from pydantic import TypeAdapter

from packages.contracts.schemas import MAX_WISH_POINTS, ScoringSettings, StrengthMultipliers, WishPoints
from packages.domain.conditions import active_rules
from packages.domain.cp_sat import generate
from packages.domain.cp_sat_score import SCORE_SCALE
from packages.domain.cp_sat_score_preferences import add_score_preferences
from packages.domain.patterns import evaluate_shapes
from packages.domain.score import schedule_score, settings_of
from packages.domain.time import aware
from tests.test_schedule_score import data, policy, shift


def _rule(key, rule, strength='medium', scope=None):
    result = policy(key, strength=strength, name=key)
    result['definition'].update(rule=rule, scope=scope or {})
    return result


def _wish(key, person, kind, first, last=None, weight=1):
    return {'id': key, 'person_id': person, 'kind': kind, 'first_day': f'2026-10-{first:02}',
            'last_day': f'2026-10-{last or first:02}', 'weight': weight}


def _snapshot(shifts, rules=(), wishes=()):
    result = data(shifts, rules)
    result['organisation']['week_start'] = 0
    for session in result['shifts']:
        session['type_snapshot'].setdefault('definition', {})
        for participant in session['participants']:
            participant.setdefault('participation', 'staff')
    result['types']['u'] = {'department_id': 'd'}
    result['workspace'] = {'department_id': 'd', 'starts_on': '2026-10-01', 'ends_on': '2026-10-12'}
    result['wishes'] = list(wishes)
    return result


def _components(snapshot, *, fixed=(), overtime=(), pairings=None):
    model = cp_model.CpModel()
    choices, participants, overtime_choices = {}, {}, {}
    by_person, commitments = defaultdict(list), defaultdict(list)
    for session in snapshot['shifts']:
        assigned = {p['person_id']: p for p in session['participants']}
        for person in snapshot['people']:
            key = session['id'], person
            participant = assigned.get(person, {'person_id': person, 'start': session['start'], 'end': session['end'],
                                                'overtime': False, 'participation': 'staff'})
            participants[key] = participant
            choice = model.new_constant(int(person in assigned)) if session['id'] in fixed else model.new_bool_var('choice')
            if session['id'] not in fixed:
                model.add(choice == int(person in assigned))
            choices[key] = choice
            start, end = aware(participant['start']), aware(participant['end'])
            if key in overtime:
                regular, extra = model.new_bool_var('regular'), model.new_bool_var('overtime')
                model.add(regular + extra == choice)
                model.add(extra == int(bool(participant.get('overtime'))))
                overtime_choices[key] = extra
                by_person[person] += [(session, {**participant, 'overtime': flag}, start, end, variable)
                                      for flag, variable in ((False, regular), (True, extra))]
            else:
                by_person[person].append((session, participant, start, end, choice))
            if person in assigned:
                commitments[person].append((session, participant, start, end))
    findings = []

    def record(code, message, session=None, person=None, start=None, end=None, severity='warning', rule=None, detail=None):
        findings.append({'code': code, 'message': message, 'shift_id': session, 'person_id': person,
                         'start': start.isoformat() if start else None, 'end': end.isoformat() if end else None,
                         'severity': severity, 'rule_id': rule['id'] if rule else None, 'detail': detail})

    resolved = {**snapshot, 'policies': active_rules(snapshot)}
    evaluate_shapes(resolved, commitments, set(snapshot['focus_ids']), record)
    pairs = {}
    for key, selected in (pairings or {}).items():
        pairs[key] = model.new_bool_var('pair')
        model.add(pairs[key] == selected)
    loss, present = add_score_preferences(model, resolved, choices, participants, pairs, overtime_choices, by_person,
                                          settings_of(snapshot.get('generation_options'))['strengths'])
    assert model.validate() == ''
    solver = cp_model.CpSolver()
    solver.parameters.num_workers = 1
    solver.parameters.max_time_in_seconds = 5
    assert solver.solve(model) == cp_model.OPTIMAL
    score = schedule_score(snapshot, {'findings': findings, 'coverage': []}, options=snapshot.get('generation_options'))
    expected = score['components']['preferences']['value']
    assert bool(solver.value(present)) == (expected is not None)
    if expected is not None:
        assert 1 - solver.value(loss) / SCORE_SCALE == pytest.approx(expected, abs=0.000051)
    return solver.value(loss) / SCORE_SCALE, findings


def test_multiple_wishes_mix_with_allocation_and_only_active_person_items():
    snapshot = _snapshot([shift('a4', 4, ['a']), shift('a5', 5, ['a']), shift('b6', 6, ['b'], 'u'), shift('b7', 7, ['b'])],
                         [_rule('avoid_t', {'kind': 'allocation_preference', 'priority': -1}, 'high', {'assignment_type_id': 't'}),
                          _rule('prefer_u', {'kind': 'allocation_preference', 'priority': 1}, 'low', {'assignment_type_id': 'u'})],
                         [_wish('a_on', 'a', 'prefer_on', 4, 6, 1 / 3), _wish('a_off', 'a', 'prefer_off', 4, 5, 2 / 3),
                          _wish('b_off', 'b', 'prefer_off', 6, weight=0.5), _wish('b_on', 'b', 'prefer_on', 7, weight=0.5)])
    loss, _ = _components(snapshot)
    a, b = (10 + 1 / 3 + 2 + 3) / 16, (10 + 1.5 + 1.5) / 17  # Missed positive opportunities add 3 for A and 1.5 for B.
    assert loss == pytest.approx(0.7 * (a + b) / 2 + 0.3 * max(a, b), abs=0.000003)


@pytest.mark.parametrize('worked_day', [4, 5])
@pytest.mark.parametrize('critical_rule', [False, True])
def test_max_budget_wish_points_retain_one_to_two_ratio_with_critical_allocation(worked_day, critical_rule):
    settings = ScoringSettings(wish_budget_points=MAX_WISH_POINTS, strengths=StrengthMultipliers(critical=10))
    points = [TypeAdapter(WishPoints).validate_python(value) for value in (1, 2)]
    wishes = [{**_wish(f'off-{day}', 'a', 'prefer_off', day, weight=value / settings.wish_budget_points),
               'points': value, 'multiplier': 1} for day, value in zip((4, 5), points)]
    rules = [_rule('critical', {'kind': 'allocation_preference', 'priority': -1}, 'critical', {'person_id': 'a'})]
    snapshot = _snapshot([shift(f's{day}', day, ['a'] if day == worked_day else []) for day in (4, 5)],
                         rules if critical_rule else [], wishes)
    snapshot['generation_options'] = {'scoring': settings.model_dump()}
    loss, _ = _components(snapshot)
    if critical_rule:
        medium, critical = settings.strengths.medium, settings.strengths.critical
        expected = (critical + medium * points[worked_day - 4] / settings.wish_budget_points) / (
            critical + medium * sum(points) / settings.wish_budget_points)
    else:
        expected = points[worked_day - 4] / sum(points)
    assert loss == pytest.approx(expected, abs=0.000001)


def test_zero_allocation_balance_weight_does_not_create_a_preference_subject():
    snapshot = _snapshot([shift('s4', 4, ['a']), shift('s5', 5, ['a'])],
                         [_rule('zero', {'kind': 'allocation_preference', 'balance_by': 'duties', 'balance_weight': 0})])
    loss, _ = _components(snapshot)
    assert loss == 0


def test_multiple_shape_breaches_count_once_per_person_and_rule():
    snapshot = _snapshot([shift('a4', 4, ['a']), shift('a6', 6, ['a', 'b']), shift('a8', 8, ['a']), shift('b5', 5, ['b'])],
                         [_rule('runs', {'kind': 'run_length', 'minimum': 2})], [_wish('off', 'a', 'prefer_off', 10)])
    loss, findings = _components(snapshot)
    assert sum(row['person_id'] == 'a' for row in findings) == 3
    assert loss == 0.325  # a: half lost; b: satisfied. Three breaches still form one item.


def test_shape_presence_keeps_independent_rules_and_narrower_scopes():
    snapshot = _snapshot([shift('t4', 4, ['a']), shift('u5', 5, ['a'], 'u')],
                         [_rule('all_run', {'kind': 'run_length', 'minimum': 2}),
                          _rule('all_gap', {'kind': 'days_off_run', 'minimum': 2}),
                          _rule('t_run', {'kind': 'run_length', 'minimum': 2}, 'high', {'assignment_type_id': 't'})])
    loss, findings = _components(snapshot)
    assert [(row['person_id'], row['rule_id'], row['code']) for row in findings] == [('a', 't_run', 'RUN_TOO_SHORT')]
    assert loss == pytest.approx(0.625, abs=0.000003)  # Only the scoped rule is broken: 10 / (3 + 3 + 10).


@pytest.mark.parametrize('overriding_person,starts_on,expected', [
    ('a', '2020-01-01', 0), ('b', '2020-01-01', 1), ('a', '2026-10-05', 1)])
def test_advisory_shape_compilation_keeps_cross_kind_override_context(overriding_person, starts_on, expected):
    from packages.domain.conditions import active_rules

    shape = _rule('short_run', {'kind': 'run_length', 'minimum': 2})
    override = _rule('personal_adjustment', {'kind': 'allocation_preference', 'priority': 0},
                     scope={'person_id': overriding_person})
    override['starts_on'] = starts_on
    override['definition']['overrides'] = [shape['id']]
    snapshot = _snapshot([shift('isolated', 4, ['a'])], [shape, override])
    snapshot['policies'] = active_rules(snapshot['policies'], context=snapshot)
    loss, findings = _components(snapshot)
    assert loss == expected
    assert any(row['code'] == 'RUN_TOO_SHORT' for row in findings) == bool(expected)


def test_fixed_history_breach_crossing_focus_day_is_scored_for_its_person():
    snapshot = _snapshot([shift('h4', 4, ['a']), shift('f5', 5, ['b']), shift('h6', 6, ['a'])],
                         [_rule('days_off', {'kind': 'days_off_run', 'minimum': 2})])
    snapshot['focus_ids'] = ['f5']
    loss, findings = _components(snapshot, fixed={'h4', 'h6'})
    assert [(row['code'], row['person_id']) for row in findings] == [('DAYS_OFF_SHORT', 'a')]
    assert loss == 0.65


@pytest.mark.parametrize('priority,extra,flag,expected', [(1, -2, False, 0), (1, -2, True, 1),
                                                         (-1, 2, False, 1), (-1, 2, True, 0)])
def test_overtime_priority_can_reverse_the_assignment_preference(priority, extra, flag, expected):
    snapshot = _snapshot([shift('s', 4, ['a'])],
                         [_rule('overtime', {'kind': 'allocation_preference', 'priority': priority, 'overtime_priority': extra})])
    snapshot['shifts'][0]['participants'][0]['overtime'] = flag
    loss, _ = _components(snapshot, overtime={('s', 'a')})
    assert loss == expected


def test_without_preference_subjects_the_component_is_absent():
    loss, _ = _components(_snapshot([shift('s', 4, ['a'])]))
    assert loss == 0


@pytest.mark.parametrize('chosen,expected', [('a', 0), ('b', 1)])
def test_positive_person_priority_scores_the_opportunity(chosen, expected):
    snapshot = _snapshot([shift('s', 4, [chosen])],
                         [_rule('preferred', {'kind': 'allocation_preference', 'priority': 50}, scope={'person_id': 'a'})])
    loss, _ = _components(snapshot)
    assert loss == expected


@pytest.mark.parametrize('preferred_assigned', [False, True])
@pytest.mark.parametrize('other_duties', [0, 1, 2])
def test_people_without_positive_opportunities_have_one_active_loss_per_person(preferred_assigned, other_duties):
    sessions = [shift('preferred', 4, ['a'] if preferred_assigned else [])]
    sessions += [shift('other-' + str(n), 5 + n, ['b']) for n in range(other_duties)]
    snapshot = _snapshot(sessions, [_rule('preferred', {'kind': 'allocation_preference', 'priority': 50},
                                         scope={'person_id': 'a'})])
    loss, _ = _components(snapshot)
    expected = (0.65 if preferred_assigned else 1) if other_duties else 0
    assert loss == pytest.approx(expected, abs=0.000003)


@pytest.mark.parametrize('chosen,lower_priority,expected', [('high', 0, 0), ('low', 0, 1),
                                                           ('high', 20, 0), ('low', 20, 0.25)])
def test_positive_session_priority_scores_the_cross_session_opportunity(chosen, lower_priority, expected):
    rules = [_rule('high', {'kind': 'allocation_preference', 'session_priority': 40},
                   scope={'assignment_type_id': 'u'})]
    if lower_priority:
        rules.append(_rule('low', {'kind': 'allocation_preference', 'session_priority': lower_priority},
                           scope={'assignment_type_id': 't'}))
    snapshot = _snapshot([shift('low', 4, ['a'] if chosen == 'low' else []),
                          shift('high', 4, ['a'] if chosen == 'high' else [], 'u')], rules)
    snapshot['people'] = {'a': snapshot['people']['a']}
    loss, _ = _components(snapshot)
    assert loss == expected


def test_positive_priority_baseline_ignores_history_outside_the_focus():
    snapshot = _snapshot([shift('focus', 4, ['a']), shift('history', 3, [], 'u')],
                         [_rule('focus', {'kind': 'allocation_preference', 'session_priority': 40},
                                scope={'assignment_type_id': 't'}),
                          _rule('history', {'kind': 'allocation_preference', 'session_priority': 100},
                                scope={'assignment_type_id': 'u'})])
    snapshot['focus_ids'] = ['focus']
    loss, _ = _components(snapshot, fixed={'history'})
    assert loss == 0


def test_scarce_cover_chooses_the_higher_priority_overlapping_session(h):
    person = h.seed['people'][0]
    duty = h.configure('assignment_types', {'name': 'Scarce cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'target': 1, 'maximum': 1}]}})['record']['id']
    h.policy({'kind': 'allocation_preference', 'priority': 0, 'session_priority': 40},
             {'service_ids': [h.seed['services'][1]]}, severity='advisory')
    first = h.shift(h.workspace(), people=[], typ=duty)
    second = h.shift(first['workspace'], people=[], typ=duty, service=1)
    snapshot = h.snapshot(second['workspace'])
    snapshot['people'] = {person: snapshot['people'][person]}
    snapshot['generation_options'] = {'shortlist_width': 0}
    result = generate(snapshot, 5, deterministic=True)
    assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
    assert result['optimality_proven'] and result['weighted_status'] == 'OPTIMAL'
    preferred = next(session for session in snapshot['shifts'] if session['service_ids'] == [h.seed['services'][1]])
    assert [(addition['shift_id'], addition['participant']['person_id']) for addition in result['additions']] == [(preferred['id'], person)]
    assert result['compiled_component_values']['preferences'] == result['objective_components']['preferences']['value'] == 1


@pytest.mark.parametrize('people,expected', [(['a', 'a'], 0.5), (['a', 'b'], 0)])
def test_configured_duty_exposure_is_shared_with_the_scorer(people, expected):
    snapshot = _snapshot([shift('s5', 5, [people[0]]), shift('s6', 6, [people[1]])],
                         [_rule('variety', {'kind': 'allocation_preference', 'balance_by': 'duties', 'balance_weight': 1})])
    loss, _ = _components(snapshot)
    assert loss == expected


@pytest.mark.parametrize('relationship', ['any', 'mentor'])
@pytest.mark.parametrize('chosen', ['b', 'c', None])
def test_supervisor_priority_uses_the_named_pair_and_canonical_relationship(relationship, chosen):
    snapshot = _snapshot([shift('s', 4, ['a', 'b', 'c'])],
                         [_rule('supervisor', {'kind': 'allocation_preference', 'priority': 10,
                                  'supervisor_relationship': relationship, 'supervisor_ids': ['b'] if relationship == 'any' else []},
                                scope={'person_id': 'a'})])
    for participant in snapshot['shifts'][0]['participants']:
        participant['participation'] = 'trainee' if participant['person_id'] == 'a' else 'supervisor'
    snapshot['shifts'][0]['participants'][0]['supervisor_person_id'] = chosen
    snapshot['mentorships'] = [{'id': 'mentor', 'person_id': 'a', 'mentor_id': 'b',
                               'starts_on': '2020-01-01', 'ends_on': None}]
    loss, _ = _components(snapshot, pairings={('s', 'a', supervisor): int(chosen == supervisor) for supervisor in ('b', 'c')})
    # No pair honours the allocation, but misses the low-strength attachment: A loses1/7, B and C lose1.
    assert loss == pytest.approx({'b': 0.766666, 'c': 1, None: 0.8}[chosen], abs=0.000002)


@pytest.mark.parametrize('start,end,priority', [('08:00', '16:00', 10), ('15:00', '16:00', 0), ('08:00', '15:00', 0)],
                         ids=['whole-duty', 'late-start', 'early-finish'])
@pytest.mark.parametrize('relationship', ['any', 'mentor'])
@pytest.mark.parametrize('named', [False, True])
def test_supervisor_allocation_credit_requires_whole_duty_presence(start, end, priority, relationship, named):
    from packages.domain.allocation_score import allocation_score_items
    from packages.domain.generation import allocation_settings

    snapshot = _snapshot([shift('s', 4, ['a', 'b'])],
                         [_rule('supervisor', {'kind': 'allocation_preference', 'priority': 10,
                                  'supervisor_relationship': relationship, 'supervisor_ids': ['b'] if relationship == 'any' else []},
                                scope={'person_id': 'a'})])
    session = snapshot['shifts'][0]
    subject, supervisor = session['participants']
    subject['supervisor_person_id'] = 'b' if named else None
    supervisor.update(participation='supervisor', start=f'2026-10-04T{start}:00+00:00', end=f'2026-10-04T{end}:00+00:00')
    if relationship == 'mentor':
        snapshot['mentorships'] = [{'id': 'mentor', 'person_id': 'a', 'mentor_id': 'b',
                                   'starts_on': '2020-01-01', 'ends_on': None}]
    resolved = {**snapshot, 'policies': active_rules(snapshot)}
    assert allocation_settings(resolved, session, 'a')[0] == priority
    assert allocation_settings(resolved, session, 'a', supervisor_id='b')[0] == priority
    [credit] = [item for item in allocation_score_items(resolved)
                if item['person_id'] == 'a' and item['label'] == 'missed positive allocation priority']
    assert credit['active'] == 1 and credit['loss'] == (0 if priority else SCORE_SCALE)
    _components(snapshot, pairings={('s', 'a', 'b'): int(named)})


def test_optional_supervision_without_a_selected_pair_keeps_public_generation_preference_parity(h, monkeypatch):
    from packages.domain import cp_sat_score_preferences

    trainee, supervisor = h.seed['people'][:2]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
        'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    duty = h.configure('assignment_types', {'name': 'Optional supervision preference', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2, 'target': 2, 'maximum': 2}], 'supervision': {
            'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
            'trainees_per_supervisor': 1, 'require_pairing': False}}})['record']['id']
    h.policy({'kind': 'allocation_preference', 'priority': -10, 'supervisor_ids': [supervisor]},
             {'person_id': trainee}, severity='advisory')
    workspace = h.shift(h.workspace(), people=[], typ=duty)['workspace']
    snapshot = h.snapshot(workspace)
    snapshot['people'] = {person: snapshot['people'][person] for person in (trainee, supervisor)}
    snapshot['wishes'] = [_wish('satisfied-supervisor-wish', supervisor, 'prefer_on', 4)]
    snapshot['generation_options'] = {'shortlist_width': 0}
    pairs, observed = {}, []
    compile_preferences = cp_sat_score_preferences.add_score_preferences
    solve = cp_model.CpSolver.solve

    def capture_pairs(model, data, choices, participants, pairings, *args, **kwargs):
        pairs.update(pairings)
        return compile_preferences(model, data, choices, participants, pairings, *args, **kwargs)

    def observe_solution(solver, model, *args, **kwargs):
        # Exercise the permitted unnamed branch without depending on a native objective tie-break.
        for pair in pairs.values():
            model.add(pair == 0)
        status = solve(solver, model, *args, **kwargs)
        if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            observed.append({key: solver.value(pair) for key, pair in pairs.items()})
        return status

    monkeypatch.setattr(cp_sat_score_preferences, 'add_score_preferences', capture_pairs)
    monkeypatch.setattr(cp_model.CpSolver, 'solve', observe_solution)
    result = generate(snapshot, 5, deterministic=True)
    assert result['status'] == 'candidate_feasible' and result['validation']['valid'], result
    assert result['validation']['findings'] == []
    assert result['weighted_status'] == 'OPTIMAL' and result['optimality_proven']
    sid = snapshot['shifts'][0]['id']
    assert observed and observed[-1] == {(sid, trainee, supervisor): 0}
    assert {addition['participant']['person_id'] for addition in result['additions']} == {trainee, supervisor}
    assert all(addition['participant'].get('supervisor_person_id') is None for addition in result['additions'])
    assert result['compiled_component_values']['preferences'] == result['objective_components']['preferences']['value'] == 0.35


@pytest.mark.parametrize('modes,target,expected', [(['virtual', 'physical'], 0.5, 0), (['virtual', 'virtual'], 0.5, 0.25),
                                                (['virtual', 'physical'], 1 / 3, 1 / 12)])
def test_service_mode_target_share_is_normalized_against_assigned_duties(modes, target, expected):
    snapshot = _snapshot([shift('s5', 5, ['a']), shift('s6', 6, ['a'])],
                         [_rule('mode_share', {'kind': 'allocation_preference', 'balance_by': 'service_mode',
                                              'balance_target_mode': 'virtual', 'balance_target_share': target})])
    snapshot['services'] = {mode: {'mode': mode} for mode in modes}
    for session, mode in zip(snapshot['shifts'], modes):
        session['service_ids'] = [mode]
    loss, _ = _components(snapshot)
    assert loss == pytest.approx(expected, abs=0.000002)


@pytest.mark.parametrize('penalty,expected', [(1, 0.75), (3, 0.625)])
def test_permitted_overtime_share_uses_its_configured_penalty(penalty, expected):
    snapshot = _snapshot([shift('s5', 5, ['a']), shift('s6', 6, ['a'])],
                         [_rule('overtime_allowed', {'kind': 'allocation_overtime', 'required_policy_ids': []})],
                         [_wish('off', 'a', 'prefer_off', 6)])
    snapshot['generation_options'] = {'overtime_penalty': penalty}
    snapshot['shifts'][0]['participants'][0]['overtime'] = True
    loss, _ = _components(snapshot, overtime={('s5', 'a'), ('s6', 'a')})
    assert loss == expected


@pytest.mark.parametrize('chosen,expected', [('b', 1), ('c', 0)])
def test_preferred_attachment_is_a_shared_preference_item(chosen, expected):
    snapshot = _snapshot([shift('s', 4, ['a', 'b', 'c'])])
    snapshot['shifts'][0]['participants'][0]['supervisor_person_id'] = chosen
    snapshot['mentorships'] = [{'id': 'preferred_attachment', 'person_id': 'a', 'mentor_id': 'c', 'strength': 'preferred',
                               'starts_on': '2020-01-01', 'ends_on': None}]
    loss, _ = _components(snapshot)
    assert loss == expected
# Include this file in hosted supervision and team-lead qualification.
