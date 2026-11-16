"""The planner's Why tab (/api/planner/why): everyone sorted into groups with the shared reasons, and score effects."""
import copy
import gc
import pytest

PATH = '/api/planner/why'


def why(h, w, shift_ids, role='coordinator', status=200, **extra):
    response = h.client(role).post(PATH, json={'workspace_id': w['id'], 'expected_revision': w['revision'], 'shift_ids': shift_ids, **extra})
    assert response.status_code == status, response.text
    return response.json()


def test_why_reuses_current_check_without_a_second_full_evaluation(h, monkeypatch):
    workspace = h.shift(h.workspace('Why current check'), people=[h.seed['people'][0]])['workspace']
    shift_id = h.detail(workspace)['shifts'][0]['id']
    from apps.api.services import staffing
    evaluate = staffing.evaluate
    def counted(*args, **kwargs):
        if kwargs.get('session_parts'):
            pytest.fail('Why re-evaluated the complete current rota check')
        return evaluate(*args, **kwargs)
    monkeypatch.setattr(staffing, 'evaluate', counted)
    result = why(h, workspace, [shift_id])
    assert result['sessions'][0]['shift_id'] == shift_id


def test_why_rejects_cached_session_findings_outside_partial_scope(h):
    from apps.api.services import staffing
    from packages.contracts.schemas import WhyInput
    from packages.domain.validation import evaluate
    present, candidate = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Why partial scope', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    w = h.workspace('Why partial scope')
    w = h.shift(w, '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00', typ=typ, people=[present])['workspace']
    w = h.shift(w, '2026-10-04T13:00:00+03:00', '2026-10-04T17:00:00+03:00', typ=typ, people=[present])['workspace']
    source = h.snapshot(w)
    stale = source['shifts'][1]
    current_id = '00000000-0000-4000-8000-000000000001'
    stale_id = '00000000-0000-4000-8000-000000000002'
    source['shifts'][0]['id'] = current_id
    stale['id'] = stale_id
    for shift in source['shifts']:
        for participant in shift['participants']:
            participant['shift_id'] = shift['id']
    source['focus_ids'] = [current_id, stale_id]
    stale['participants'].append({**copy.deepcopy(stale['participants'][0]), 'id': 'stale-participant', 'person_id': candidate})
    previous = evaluate(source, explain_findings=False, session_parts=True)
    previous_ids = {previous['findings'][index]['shift_id'] for index in previous['_session_parts']
                    if previous['findings'][index].get('shift_id')}
    current = source['shifts'][0]['id']
    assert stale['id'] in previous_ids, previous['findings']
    print('partial finding scope', {'previous_shift_ids': sorted(previous_ids),
                                    'current_shift_ids': [current],
                                    'missing_shift_ids': sorted(previous_ids - {current})})
    scoped = {**source, 'shifts': [source['shifts'][0]], 'focus_ids': [current], '_why_validation': previous}
    result = staffing.why(scoped, {}, WhyInput(workspace_id=w['id'], expected_revision=w['revision'],
                                               shift_ids=[current], person_id=candidate))
    assert result['sessions'][0]['shift_id'] == current


def test_why_does_not_return_fill_options_reason_short(h):
    workspace = h.shift(h.workspace('Why response contract'), people=[h.seed['people'][0]])['workspace']
    shift_id = h.detail(workspace)['shifts'][0]['id']
    result = why(h, workspace, [shift_id])
    assert all('reason_short' not in row for row in result['sessions'][0]['rows'])


@pytest.mark.parametrize('over_budget', [False, True])
def test_why_keeps_session_safety_for_scored_and_over_budget_rows(h, monkeypatch, over_budget):
    from apps.api.services import staffing
    candidate, present = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Why session safety', 'starts_on': '2020-01-01',
                    'definition': {'requirements': [{'minimum': 1, 'maximum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace('Why session safety'), typ=typ, people=[present])['workspace']
    shift_id = h.detail(workspace)['shifts'][0]['id']
    if over_budget:
        monkeypatch.setattr(staffing, 'WHY_SCORE_WORK', 0)
    session = why(h, workspace, [shift_id], person_id=candidate)['sessions'][0]
    row = next(row for row in session['rows'] if row['person_id'] == candidate)
    assert row['group'] == 'rule'
    assert any(finding['code'] == 'MAXIMUM_PARTICIPANTS' for finding in row['findings'])
    assert session['score_status'] == ('work_limit' if over_budget else 'complete')


def test_why_groups_everyone_for_a_session_and_one_person_across_sessions(h):
    rest = h.policy({'kind': 'min_rest', 'hours': 12})
    on, tired, away = h.seed['people'][:3]
    w = h.workspace('Why', '2026-10-04', '2026-10-11')
    w = h.shift(w, '2026-10-04T08:00:00+03:00', '2026-10-04T20:00:00+03:00', people=[tired])['workspace']
    w = h.shift(w, '2026-10-04T22:00:00+03:00', '2026-10-05T06:00:00+03:00', people=[on], location=1)['workspace']
    h.command('coordinator', 'availability.record', {'person_id': away, 'kind': 'recorded_leave', 'start': '2026-10-04T00:00:00+03:00',
                                                     'end': '2026-10-06T00:00:00+03:00', 'private_reason': 'Private leave reason'})
    w = h.detail(w)['workspace']
    night = next(s for s in h.detail(w)['shifts'] if s['participants'] and s['participants'][0]['person_id'] == on)
    result = why(h, w, [night['id']])
    assert 'Private leave reason' not in str(result)
    rows = {r['person_id']: r for r in result['sessions'][0]['rows']}
    assert rows[on]['group'] == 'assigned' and rows[on]['assignment_id'] == night['participants'][0]['id']
    assert rows[tired]['group'] == 'rule' and any(f['code'] == 'MIN_REST' and f['rule_id'] == rest['id'] and
                                                  f['kind'] == 'REST' and f['detail']['required_hours'] == 12
                                                  for f in rows[tired]['findings'])
    assert rows[away]['group'] == 'unavailable'
    groups = [r['group'] for r in result['sessions'][0]['rows']]
    order = ['assigned', 'free', 'rule', 'unavailable', 'not_eligible']
    assert groups == sorted(groups, key=order.index)
    scored = [r for r in result['sessions'][0]['rows'] if 'effect' in r]
    assert scored and all(r['group'] in {'assigned', 'free'} for r in scored) and len(scored) <= result['scored_limit']
    assert {'points', 'coverage', 'rules', 'fairness', 'preferences', 'stability', 'gate', 'other', 'wishes'} <= set(scored[0]['effect'])
    assert result['sessions'][0]['runner_up'] is None
    for row in scored:
        effect = row['effect']
        assert round(sum(effect[k] for k in ('coverage', 'rules', 'fairness', 'preferences', 'stability', 'gate', 'other')), 1) == effect['points']
        assert effect['points'] == round(effect['score_with'] - effect['score_without'], 1)
        assert effect['stability'] == 0

    # One person across the rota's sessions: one row per session, in time order, assigned where they work.
    shifts = h.detail(w)['shifts']
    person = why(h, w, [s['id'] for s in shifts], person_id=on)
    assert [len(s['rows']) for s in person['sessions']] == [1] * len(shifts)
    assert [s['shift_id'] for s in person['sessions']] == [s['id'] for s in sorted(shifts, key=lambda s: (s['start'], s['id']))]
    assert 'assigned' in {s['rows'][0]['group'] for s in person['sessions']}
    assert all(r['person_id'] == on for s in person['sessions'] for r in s['rows'])
    assert all(s['runner_up'] is None for s in person['sessions'])


@pytest.mark.parametrize('assigned', [False, True])
def test_why_explains_first_or_last_optional_assignment(h, assigned):
    from packages.domain.score import COMPONENTS, schedule_score
    from packages.domain.validation import evaluate

    person = h.seed['people'][0]
    typ = h.configure('assignment_types', {'name': 'Optional score-effect fixture', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1}]}})['record']
    workspace = h.shift(h.workspace(), typ=typ['id'], people=[person] if assigned else [])['workspace']
    source = h.snapshot(workspace)
    session = source['shifts'][0]
    checked = evaluate(source)
    assert checked['valid'], checked['findings']
    assert schedule_score(source, checked)['score'] == (100 if assigned else 0)
    result = why(h, workspace, [session['id']], person_id=person)['sessions'][0]
    assert result['score_status'] == 'complete'
    [row] = result['rows']
    assert row['group'] == ('assigned' if assigned else 'free')
    effect = row['effect']
    # Why always compares with the person against without, for both the Assign and Remove rows.
    assert effect['points'] == effect['other'] == 100
    assert effect['score_with'] == 100 and effect['score_without'] == 0
    assert all(effect[key] == 0 for key in (*COMPONENTS, 'gate'))
    assert round(sum(effect[key] for key in (*COMPONENTS, 'gate', 'other')), 1) == effect['points']


@pytest.mark.parametrize('person_mode, runner_up', [(True, None), (True, False), (False, True)])
def test_runner_up_requires_opt_in_and_a_person(h, monkeypatch, person_mode, runner_up):
    from apps.api.services import staffing
    pid = h.seed['people'][0]
    w = h.shift(h.workspace(), people=[pid])['workspace']
    sid = h.detail(w)['shifts'][0]['id']
    calls, original = [], staffing.check_people

    def checked(source, shift, replaced, candidates, **kwargs):
        calls.append(set(candidates))
        return original(source, shift, replaced, candidates, **kwargs)

    monkeypatch.setattr(staffing, 'check_people', checked)
    options = {'person_id': pid} if person_mode else {}
    if runner_up is not None:
        options['runner_up'] = runner_up
    session = why(h, w, [sid], **options)['sessions'][0]
    assert session['runner_up'] is None and session['score_status'] == 'complete'
    assert calls == [{pid} if person_mode else set(h.seed['people'])]


@pytest.mark.parametrize('assigned', [True, False])
def test_person_runner_up_is_the_best_session_alternative(h, monkeypatch, assigned):
    from apps.api.services import staffing
    from packages.domain.score import schedule_score
    from packages.domain.validation import evaluate
    a, b, c = h.seed['people'][:3]
    w = h.shift(h.workspace(), people=[a] if assigned else [])['workspace']
    source = h.snapshot(w)
    shift = source['shifts'][0]
    source['wishes'] = [{'id': 'want-on', 'person_id': c, 'kind': 'prefer_on', 'first_day': '2026-10-04',
                         'last_day': '2026-10-04', 'weight': 1}]
    monkeypatch.setattr(staffing, 'domain_snapshot', lambda *_: source)
    # Independently score each actual replacement, including its global normalization.
    scores = {}
    for pid in (a, b, c):
        trial = {**source, 'shifts': [{**shift, 'participants': [{'id': 'trial', 'person_id': pid,
                 'shift_id': shift['id'], 'start': shift['start'], 'end': shift['end'], 'participation': 'staff'}]}]}
        scores[pid] = schedule_score(trial, evaluate(trial))
    assert scores[c]['score'] > scores[b]['score']
    session = why(h, w, [shift['id']], person_id=a, runner_up=True)['sessions'][0]
    assert session['score_status'] == 'complete'
    runner = session['runner_up']
    assert runner['person_id'] == c
    assert runner['point_difference'] == round(scores[a]['score'] - scores[c]['score'], 1)
    assert runner['uncapped_point_difference'] == round(scores[a]['uncapped_score'] - scores[c]['uncapped_score'], 1)
    assert session['rows'][0]['effect']['score_with'] == scores[a]['score']


@pytest.mark.parametrize('block', ['inactive', 'eligibility', 'leave', 'rule', 'selected'])
def test_runner_up_never_offers_an_ineligible_person(h, monkeypatch, block):
    from apps.api.services import staffing
    a, b, c = h.seed['people'][:3]
    if block == 'rule':
        h.policy({'kind': 'restriction', 'weekdays': [6], 'start_time': '00:00', 'end_time': '24:00'}, {'person_id': c})
    w = h.shift(h.workspace(), people=[a] if block != 'selected' else [])['workspace']
    source = h.snapshot(w)
    shift = source['shifts'][0]
    if block == 'inactive':
        source['people'][c]['active'] = False
    elif block == 'eligibility':
        source['eligibilities'] = [r for r in source['eligibilities'] if r['person_id'] != c]
    elif block in {'leave', 'selected'}:
        source['availability'].append({'id': 'synthetic-leave', 'person_id': a if block == 'selected' else c, 'start': shift['start'],
                                       'end': shift['end'], 'kind': 'recorded_leave'})
    source['wishes'] = [{'id': 'want-on', 'person_id': c, 'kind': 'prefer_on', 'first_day': '2026-10-04',
                         'last_day': '2026-10-04', 'weight': 1}]
    monkeypatch.setattr(staffing, 'domain_snapshot', lambda *_: source)
    session = why(h, w, [shift['id']], person_id=a, runner_up=True)['sessions'][0]
    assert session['score_status'] == 'complete'
    assert session['runner_up']['person_id'] == (c if block == 'selected' else b)
    if block == 'selected':
        assert session['rows'][0]['group'] == 'unavailable'


def test_why_equal_loads_use_per_session_shuffle(h, monkeypatch):
    from apps.api.services import staffing
    old, *candidates = h.seed['people']
    workspace = h.shift(h.workspace(), people=[old])['workspace']
    source = h.snapshot(workspace)
    for pid, name in zip(sorted(candidates), ('Zulu', 'Alpha')):
        source['people'][pid]['display_name'] = name
    monkeypatch.setattr(staffing, 'domain_snapshot', lambda *_: source)
    session = why(h, workspace, [source['shifts'][0]['id']])['sessions'][0]
    free = [row for row in session['rows'] if row['group'] == 'free']
    assert [row['person_id'] for row in free] == sorted(
        candidates, key=lambda pid: staffing.best_order_key(0, 0, pid, session['shift_id']))
    assert all(row['week_hours'] == row['rota_hours'] == 0 for row in free)


def test_runner_up_ties_are_stable_and_absence_is_explicit(h, monkeypatch):
    from apps.api.services import staffing
    a, b, c = h.seed['people'][:3]
    w = h.shift(h.workspace(), people=[a])['workspace']
    source = h.snapshot(w)
    monkeypatch.setattr(staffing, 'domain_snapshot', lambda *_: source)
    sid = source['shifts'][0]['id']
    session = why(h, w, [sid], person_id=a, runner_up=True)['sessions'][0]
    assert session['runner_up']['person_id'] == min(b, c)
    source['people'][b]['active'] = source['people'][c]['active'] = False
    session = why(h, w, [sid], person_id=a, runner_up=True)['sessions'][0]
    assert session['runner_up'] is None and session['score_status'] == 'complete'


@pytest.mark.parametrize('case', ['ordinary', 'service', 'service_group', 'supervision', 'shape_resources'])
def test_why_score_trials_recheck_only_simultaneous_sessions(h, monkeypatch, case):
    import copy
    from datetime import timedelta
    import uuid
    from apps.api.services import staffing
    from packages.contracts.schemas import PolicyDefinition, WhyInput
    from packages.domain import validation
    from packages.domain.score import schedule_score
    from packages.domain.time import aware
    from tests.test_planning_incremental import canonical

    selected, supervisor, alternative = h.seed['people']
    if case == 'supervision':
        selected, supervisor = supervisor, selected
    w = h.shift(h.workspace(), people=[selected])['workspace']
    source = h.snapshot(w)
    target = source['shifts'][0]
    for name, delta, person in (('peer', timedelta(hours=2), supervisor),
                                ('next', timedelta(days=1), selected),
                                ('context', timedelta(days=2), alternative),
                                ('closed', timedelta(days=3), selected)):
        row = copy.deepcopy(target)
        row.update(id=str(uuid.uuid5(uuid.UUID(target['id']), name)),
                   start=(aware(target['start']) + delta).isoformat(),
                   end=(aware(target['end']) + delta).isoformat(), closed=name == 'closed')
        row['participants'] = [{**row['participants'][0], 'id': str(uuid.uuid4()), 'shift_id': row['id'],
                                'person_id': person, 'start': row['start'], 'end': row['end']}]
        source['shifts'].append(row)
        if name != 'context':
            source['focus_ids'].append(row['id'])
    rules = [({'kind': 'max_duties', 'maximum': 1, 'window': 'week'}, 'advisory'),
             ({'kind': 'block_quota', 'metric': 'duties', 'comparison': 'target', 'amount': 2,
               'starts_on': '2026-10-04', 'ends_on': '2026-10-11'}, 'advisory'),
             ({'kind': 'balance_goal', 'metric': 'duties', 'aim': 'equal_share'}, 'advisory')]
    if case in {'service', 'service_group'}:
        rules.append(({'kind': 'maximum_participants', 'maximum': 1, 'aggregation': case}
                      if case == 'service' else {'kind': 'minimum_cover', 'minimum': 2, 'aggregation': case}, 'mandatory'))
    if case == 'shape_resources':
        rules.append(({'kind': 'run_length', 'minimum': 2}, 'advisory'))
        for row in source['shifts']:
            row['type_snapshot']['definition']['resources'] = [
                {'resource_pool_id': 'missing-pool', 'units': 1, 'from_minute': 0}]
    if case == 'supervision':
        peer = source['shifts'][1]
        peer['participants'].append({**peer['participants'][0], 'id': str(uuid.uuid4()),
                                     'person_id': selected, 'participation': 'supervisor'})
        for row in source['shifts']:
            row['type_snapshot']['definition']['supervision'] = {
                'trainee_category_ids': [h.seed['trainee_category']],
                'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1}
    for rule, severity in rules:
        source['policies'].append({'id': str(uuid.uuid4()), 'name': 'Why full-check parity',
            'kind': rule['kind'], 'revision': 1, 'department_id': w['department_id'],
            'status': 'active', 'active': True, 'starts_on': '2020-01-01',
            'definition': PolicyDefinition(rule=rule, severity=severity,
                scope={'service_ids': h.seed['services']} if rule.get('aggregation') else {}).model_dump(mode='json')})
    source['wishes'] = [{'id': 'why-wish', 'person_id': alternative, 'kind': 'prefer_on',
                         'first_day': '2026-10-04', 'last_day': '2026-10-06', 'weight': 2}]
    touched = {row['id'] for row in source['shifts'][:2]}
    checked, indexes, partials = [], [], []
    original_setup, original_evaluate, original_index = validation.validate_session_setup, validation.evaluate, staffing.people_index

    def setup(data, shift, *args):
        checked.append(shift['id'])
        return original_setup(data, shift, *args)

    def evaluate(data, **kwargs):
        before = len(checked)
        cached = bool(kwargs.get('session_cache'))
        result = original_evaluate(data, **kwargs)
        if data.get('_partial'):
            refreshed = set(checked[before:])
            assert target['id'] in refreshed <= touched
            if case in {'service', 'service_group', 'supervision'}:
                assert refreshed == touched
            elif cached:
                assert refreshed == {target['id']}
            partials.append(data['_partial'])
            raw = {**source, 'shifts': data['shifts']}
            full = original_evaluate(raw, explain_findings=False)
            assert canonical(result) == canonical(full)
            for options in (None, {'scoring': {'weights': {'coverage': 1, 'rules': 2, 'fairness': 3,
                                                          'preferences': 4, 'stability': 0}}}):
                assert schedule_score(data, result, options, limit=0) == schedule_score(raw, full, options, limit=0)
        return result

    def indexed(data):
        indexes.append(1)
        return original_index(data)

    monkeypatch.setattr(validation, 'validate_session_setup', setup)
    monkeypatch.setattr(validation, 'evaluate', evaluate)
    monkeypatch.setattr(staffing, 'evaluate', evaluate)
    monkeypatch.setattr(staffing, 'people_index', indexed)
    for mode in ('session', 'person', 'runner_up'):
        indexes.clear()
        partials.clear()
        value = WhyInput(workspace_id=w['id'], expected_revision=w['revision'], shift_ids=[target['id']],
                         person_id=selected if mode != 'session' else None, runner_up=mode == 'runner_up')
        result = staffing.why(copy.deepcopy(source), {}, value)
        assert result['sessions'][0]['score_status'] == 'complete'
        assert indexes == [1] and partials
        assert all(part['complete'] and part['shifts'] == touched for part in partials)


@pytest.fixture(scope='module')
def why_department_scale(scale_harness):
    """Measure ranking, default effects and overload handling once for both test kinds."""
    import time
    from apps.api.services import staffing
    from packages.domain import score, validation
    from packages.domain.validation import evaluate
    from tests.scheduler_departments import department
    h = scale_harness('h')
    with pytest.MonkeyPatch.context() as monkeypatch:
        data, _ = department(h, 7, people=150, days=28, rules='full', leave_rate=0.05, clinics=9)
        w, target = data['workspace'], data['shifts'][0]
        pid = h.seed['people'][0]
        target['participants'] = [{'id': h.seed['people'][1], 'person_id': pid, 'shift_id': target['id'],
                                   'start': target['start'], 'end': target['end'], 'participation': 'staff'}]
        monkeypatch.setattr(staffing, 'domain_snapshot', lambda *_: data)
        # Warm the read-only request before measuring it, independent of the worker's earlier tests.
        why(h, w, [target['id']], person_id=pid, runner_up=True)
        staffing._WHY_RESULT_CACHE.clear()
        gc.collect()
        calls, samples, original = [], [], score.schedule_score
        session_checks, indexes, measurements = [], [], {}
        original_setup, original_index = validation.validate_session_setup, staffing.people_index

        def setup(snapshot, shift, *args):
            if len(snapshot['shifts']) == len(data['shifts']) and not snapshot.get('skip_coverage'):
                session_checks.append(shift['id'])
            return original_setup(snapshot, shift, *args)

        def indexed(source):
            indexes.append(1)
            return original_index(source)

        monkeypatch.setattr(validation, 'validate_session_setup', setup)
        monkeypatch.setattr(staffing, 'people_index', indexed)

        def counted(trial, *args, **kwargs):
            calls.append(len(trial['shifts']))
            result = original(trial, *args, **kwargs)
            if len(samples) < 5:
                samples.append((trial, args, kwargs, result))
            return result

        monkeypatch.setattr(score, 'schedule_score', counted)
        started = time.process_time()
        session = why(h, w, [target['id']], person_id=pid, runner_up=True)['sessions'][0]
        elapsed = time.process_time() - started
        ranked = session, list(calls), elapsed
        measurements['ranked'] = {'session_checks': len(session_checks), 'indexes': len(indexes)}
        print('why department scale', {'people': len(data['people']), 'sessions': len(data['shifts']),
                                        'score_trials': len(calls), 'cpu_seconds': round(elapsed, 2)})
        # Compute independent full checks outside the timing window and before later scenario mutations.
        sample_checks = []
        for trial, args, kwargs, result in samples[2:]:
            uncached = {key: value for key, value in trial.items()
                        if not key.endswith('_cache') and key not in {
                            '_partial', '_person_rows', '_fair_share_eligibility', '_policy_applies_people',
                            '_active_policies', '_counted_policies', '_scope_membership_index',
                            '_quota_subject_context', '_quota_subject_context_budget'}}
            # Compare every constraint/score input in the same undecorated mode used by scoring trials.
            full_validation = evaluate(uncached, explain_findings=False)
            sample_checks.append((args[0], full_validation, result,
                                  original(uncached, full_validation, *args[1:], **kwargs)))

        calls.clear()
        session_checks.clear()
        indexes.clear()
        gc.collect()
        started = time.process_time()
        session = why(h, w, [target['id']])['sessions'][0]
        session_mode = session, list(calls), time.process_time() - started
        measurements['session'] = {'session_checks': len(session_checks), 'indexes': len(indexes)}
        print('why department scale session', {'score_trials': len(calls), 'cpu_seconds': round(session_mode[2], 2)})

        # Effects can fit while ranking cannot: never label a partial candidate ranking complete.
        cost = (len(data['shifts']) + 1 + len(data['people']) * (len(data['policies']) + 1))
        monkeypatch.setattr(staffing, 'WHY_SCORE_WORK', 2 * cost)
        calls.clear()
        session_checks.clear()
        indexes.clear()
        gc.collect()
        started = time.process_time()
        session = why(h, w, [target['id']], person_id=pid)['sessions'][0]
        elapsed = time.process_time() - started
        default = session, list(calls), elapsed
        measurements['default'] = {'session_checks': len(session_checks), 'indexes': len(indexes)}
        print('why department scale default', {'score_trials': len(calls), 'cpu_seconds': round(elapsed, 2)})

        calls.clear()
        session = why(h, w, [target['id']], person_id=pid, runner_up=True)['sessions'][0]
        limited = session, list(calls)

        monkeypatch.setattr(staffing, 'WHY_SCORE_WORK', 0)
        calls.clear()
        session = why(h, w, [target['id']], person_id=pid, runner_up=True)['sessions'][0]
        disabled = session, list(calls)
        session_checks.clear()
        indexes.clear()
        gc.collect()
        started = time.process_time()
        sessions = why(h, w, [s['id'] for s in data['shifts'][:200]], person_id=pid, runner_up=True)['sessions']
        bulk = sessions, list(calls), time.process_time() - started
        measurements['bulk'] = {'session_checks': len(session_checks), 'indexes': len(indexes)}

        # A large directory with one eligible alternative needs only three scoring passes.
        for person in data['people'].values():
            person['active'] = person['id'] in {pid, h.seed['people'][1]}
        monkeypatch.setattr(staffing, 'WHY_SCORE_WORK', 3 * cost)
        staffing._WHY_SOURCE_CACHE.clear()
        staffing._WHY_RESULT_CACHE.clear()
        session = why(h, w, [target['id']], person_id=pid, runner_up=True)['sessions'][0]
        small = session, list(calls)
        return len(data['people']), len(data['shifts']), ranked, sample_checks, default, limited, disabled, bulk, small, session_mode, measurements


def test_why_stays_fast_at_department_scale(why_department_scale):
    people, shifts, ranked, sample_checks, default, limited, disabled, bulk, small, session_mode, measurements = why_department_scale
    assert shifts >= 500 and people >= 150
    session, calls, _ = ranked
    assert session['score_status'] == 'complete' and session['runner_up'] is not None
    assert len(calls) <= people + 1
    assert set(calls) == {shifts}, 'Keep full-rota normalization for each necessary score'
    for validation, full_validation, result, full_result in sample_checks:
        assert validation == full_validation
        assert result == full_result
    session, calls, _ = default
    assert session['score_status'] == 'complete' and session['runner_up'] is None
    assert 'effect' in session['rows'][0] and len(calls) <= 2
    assert set(calls) == {shifts}
    session, calls = limited
    assert session['score_status'] == 'work_limit' and session['runner_up'] is None
    assert len(calls) <= 2
    session, calls = disabled
    assert session['score_status'] == 'work_limit' and session['runner_up'] is None and not calls
    sessions, calls, _ = bulk
    assert len(sessions) == 200 and all(s['score_status'] == 'work_limit' for s in sessions) and not calls
    session, calls = small
    assert session['score_status'] == 'complete' and session['runner_up'] is not None
    assert len(calls) <= 3
    session, calls, _ = session_mode
    assert len(session['rows']) == people and session['runner_up'] is None
    assert sum('effect' in row for row in session['rows']) == 12 and len(calls) == 13
    assert set(calls) == {shifts}
    assert all(row['indexes'] == 1 for row in measurements.values())
    assert measurements['default']['session_checks'] < 2 * shifts
    assert measurements['session']['session_checks'] < 2 * shifts


@pytest.mark.cpu_budget
def test_why_stays_fast_at_department_scale_cpu_budget(why_department_scale, record_property):
    from tests.test_scheduler_performance import _budget
    _, _, ranked, _, default, _, _, bulk, _, session_mode, measurements = why_department_scale
    for name, measured in (('ranked', ranked), ('default', default), ('bulk', bulk), ('session', session_mode)):
        record_property(name + '_cpu_seconds', measured[2])
        record_property(name + '_score_trials', len(measured[1]))
        for key, value in measurements[name].items():
            record_property(name + '_' + key, value)
    assert ranked[2] < _budget(15)
    assert default[2] < _budget(2)
    assert bulk[2] < _budget(10)
    assert session_mode[2] < _budget(5)


def test_why_is_read_only_for_reviewers_and_refuses_bad_requests(h):
    w = h.workspace('Why access', '2026-10-04', '2026-10-11')
    w = h.shift(w)['workspace']
    sid = h.detail(w)['shifts'][0]['id']
    assert why(h, w, [sid], role='approver')['sessions'][0]['shift_id'] == sid
    why(h, w, [sid], role='staff', status=403)
    why(h, w, [sid, sid], status=422)  # several sessions need a person
    why(h, w, ['00000000-0000-4000-8000-000000000000'], status=404)
    why(h, w, [sid], person_id='00000000-0000-4000-8000-000000000000', status=404)
    why(h, {**w, 'revision': w['revision'] + 5}, [sid], status=409)
