"""Same-evaluation qualification reuse keeps complete candidate evidence and live reports."""
from collections import Counter
from copy import deepcopy

import pytest

from packages.domain import candidate_evidence as candidates, score, validation
from packages.domain.time import aware
from tests.test_schedule_score import data, shift


def evaluated_view(snapshot):
    cache = {}
    return {**snapshot, '_qualification_cache': cache, '_qualification_evidence_cache': cache}


def uncached_evidence(context, pid, session):
    return candidates.candidate_evidence({k: v for k, v in context.items()
                                         if k != '_qualification_evidence_cache'}, pid, session)


def count_qualification(monkeypatch):
    calls = []
    canonical = validation.qualification_failures

    def counted(*args):
        calls.append(1)
        return canonical(*args)

    monkeypatch.setattr(validation, 'qualification_failures', counted)
    monkeypatch.setattr(candidates, 'qualification_failures', counted)
    return calls


def test_successful_qualification_reuses_only_its_empty_evidence(monkeypatch):
    session = shift('first', 5, [])
    snapshot = data([session])
    snapshot['policies'] = []
    view = evaluated_view(snapshot)
    calls = count_qualification(monkeypatch)
    assert validation.qualified(view, 'a', session, session['type_snapshot']['definition'],
                                aware(session['start']), aware(session['end']))
    assert len(calls) == 1
    expected = uncached_evidence(snapshot, 'a', session)
    calls.clear()
    assert candidates.candidate_evidence(view, 'a', session) == expected
    peer = {**session, 'id': 'second'}
    assert candidates.candidate_evidence(view, 'a', peer) == {**expected, 'shift_id': 'second'}
    assert calls == []
    view['availability'] = [{'id': 'leave', 'person_id': 'a',
                             'start': session['start'], 'end': session['end']}]
    assert candidates.candidate_evidence(view, 'a', session) == uncached_evidence(view, 'a', session)
    assert candidates.candidate_evidence(view, 'a', session)['exclusions'] == [
        {'code': 'UNAVAILABLE', 'record_id': 'leave', 'start': session['start'], 'end': session['end']}]
    view['availability'].clear()
    session['series_snapshot'] = {'definition': {'usual_assignees_fixed': True, 'default_person_ids': ['b']}}
    assert candidates.candidate_evidence(view, 'a', session)['exclusions'] == [
        {'code': 'USUAL_ASSIGNEES_FIXED', 'series_id': None}]
    session['participants'] = [{'person_id': 'a'}]
    assert candidates.candidate_evidence(view, 'a', session)['eligible_and_available']


@pytest.mark.parametrize('dimension', ['person', 'department', 'services', 'ranks', 'start', 'end'])
def test_qualification_evidence_reuse_requires_the_complete_existing_key(monkeypatch, dimension):
    session = shift('key', 5, [])
    snapshot = data([session])
    snapshot['policies'] = []
    view = evaluated_view(snapshot)
    assert validation.qualified(view, 'a', session, session['type_snapshot']['definition'],
                                aware(session['start']), aware(session['end']))
    pid, edited = 'a', deepcopy(session)
    if dimension == 'person':
        pid = 'b'
    elif dimension == 'department':
        view['types'] = {**view['types'], 'other-type': {'department_id': 'other-department'}}
        edited['assignment_type_id'] = 'other-type'
    elif dimension == 'services':
        edited['service_ids'] = ['other-clinic']
    elif dimension == 'ranks':
        edited['type_snapshot']['definition']['allowed_category_ids'] = ['other-rank']
    elif dimension == 'start':
        edited['start'] = '2026-10-05T09:00:00+00:00'
    else:
        edited['end'] = '2026-10-05T15:00:00+00:00'
    expected = uncached_evidence(view, pid, edited)
    calls = count_qualification(monkeypatch)
    assert candidates.candidate_evidence(view, pid, edited) == expected
    assert len(calls) == 1


def test_standalone_evidence_keeps_canonical_checks_despite_a_supplied_boolean_cache():
    session = shift('standalone', 5, [])
    snapshot = data([session])
    snapshot['policies'] = []
    snapshot['_qualification_cache'] = {}
    assert validation.qualified(snapshot, 'a', session, session['type_snapshot']['definition'],
                                aware(session['start']), aware(session['end']))
    snapshot['people']['a']['active'] = False
    assert candidates.candidate_evidence(snapshot, 'a', session)['exclusions'] == [
        {'code': 'PERSON_INACTIVE', 'person_id': 'a'}]


def test_negative_qualification_keeps_full_fresh_independent_explanations(monkeypatch):
    session = shift('negative', 5, [])
    snapshot = data([session])
    snapshot['policies'] = []
    session['service_ids'] = ['clinic']
    snapshot['memberships'] = []
    view = evaluated_view(snapshot)
    assert not validation.qualified(view, 'a', session, session['type_snapshot']['definition'],
                                    aware(session['start']), aware(session['end']))
    expected = uncached_evidence(view, 'a', session)
    calls = count_qualification(monkeypatch)
    actual = candidates.candidate_evidence(view, 'a', session)
    assert actual == expected and len(calls) == 1
    assert [r['code'] for r in actual['exclusions']] == ['MEMBERSHIP_MISSING', 'ELIGIBILITY_MISSING']
    actual['exclusions'][1]['existing_record_ids'].append('do-not-retain')
    actual['exclusions'].append({'code': 'do-not-retain'})
    assert candidates.candidate_evidence(view, 'a', session) == expected


@pytest.mark.parametrize('malformed', ['type', 'services', 'ranks'])
def test_cache_probe_keeps_canonical_malformed_input_results_and_errors(malformed):
    session = shift('malformed', 5, [])
    snapshot = data([session])
    snapshot['policies'] = []
    if malformed == 'type':
        session['assignment_type_id'] = 'missing'
    elif malformed == 'services':
        session['service_ids'] = [['not-a-service-id']]
    else:
        session['type_snapshot']['definition']['allowed_category_ids'] = [['not-a-rank-id']]
    snapshot['_qualification_evidence_cache'] = {'unrelated': True}

    def outcome(view):
        try:
            return ('result', candidates.candidate_evidence(view, 'a', session))
        except (KeyError, TypeError) as error:
            return (type(error), error.args)

    assert outcome(snapshot) == outcome({k: v for k, v in snapshot.items()
                                         if k != '_qualification_evidence_cache'})


@pytest.mark.parametrize('explain_findings', [False, True])
def test_twenty_immutable_candidate_trials_keep_full_validation_score_and_reports(h, monkeypatch, explain_findings):
    from tests.test_solver_supervision_cache import assignment, snapshot

    source, _, _, _, independent, trainee, supervisor, other = snapshot(h)
    original = deepcopy(source)
    cache, canonical, calls = {}, candidates.candidate_evidence, Counter()
    mode = 'actual'
    failures = candidates.qualification_failures

    def counted(*args):
        calls[mode] += 1
        return failures(*args)

    def reference(context, *args):
        return canonical({k: v for k, v in context.items() if k != '_qualification_evidence_cache'}, *args)

    monkeypatch.setattr(candidates, 'qualification_failures', counted)
    trials = [source]
    for n in range(20):
        person = (supervisor, trainee, other)[n % 3]
        changed = {**independent, 'participants': [assignment(independent, person, id=f'trial-{n}')]}
        trials.append({**source, 'shifts': [changed if row is independent else row for row in source['shifts']]})
    for view in trials:
        mode = 'reference'
        with monkeypatch.context() as patch:
            patch.setattr(candidates, 'candidate_evidence', reference)
            expected = validation.evaluate(view, explain_findings=explain_findings)
            expected_score = score.schedule_score(view, expected)
        mode = 'actual'
        actual = validation.evaluate(view, session_cache=cache, explain_findings=explain_findings)
        assert actual == expected
        assert score.schedule_score(view, actual) == expected_score
    assert calls['actual'] < calls['reference']
    assert source == original and '_qualification_evidence_cache' not in source


@pytest.mark.parametrize('edit', ['active', 'membership', 'rank', 'eligibility', 'leave', 'rotation', 'grant'])
def test_each_evaluation_refreshes_qualification_and_complete_reports_after_fact_edits(h, monkeypatch, edit):
    from tests.test_solver_supervision_cache import snapshot

    source, first, _, _, _, trainee, supervisor, _ = snapshot(h)
    source = evaluated_view(source)
    assert validation.qualified(source, trainee, first, first['type_snapshot']['definition'],
                                aware(first['start']), aware(first['end']))
    validation.evaluate(source)
    if edit == 'active':
        source['people'][trainee]['active'] = False
    elif edit == 'membership':
        source['memberships'] = [r for r in source['memberships'] if r['person_id'] != trainee]
    elif edit == 'rank':
        first['type_snapshot']['definition']['allowed_category_ids'] = ['00000000-0000-0000-0000-000000000099']
    elif edit == 'eligibility':
        source['eligibilities'] = [r for r in source['eligibilities'] if r['person_id'] != trainee]
    elif edit == 'leave':
        source['availability'].append({'id': 'leave', 'person_id': trainee,
                                      'start': first['start'], 'end': first['end'], 'kind': 'recorded_leave'})
    elif edit == 'rotation':
        source.setdefault('rotations', []).append({'id': 'away', 'person_id': trainee,
            'department_available': False, 'starts_on': '2020-01-01', 'ends_on': None})
    else:
        source['grants'] = [r for r in source['grants'] if r['person_id'] != supervisor]
    original = deepcopy(source)
    canonical = candidates.candidate_evidence

    def reference(context, *args):
        return canonical({k: v for k, v in context.items() if k != '_qualification_evidence_cache'}, *args)

    public = {k: v for k, v in source.items()
              if k not in ('_qualification_cache', '_qualification_evidence_cache')}
    actual = validation.evaluate(source)
    with monkeypatch.context() as patch:
        patch.setattr(candidates, 'candidate_evidence', reference)
        expected = validation.evaluate(source)
        expected_score = score.schedule_score(public, expected)
    assert actual == expected and score.schedule_score(public, actual) == expected_score
    assert source == original
