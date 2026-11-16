"""Fixed preparation stays evaluation-local; all candidate and clinical outputs remain fresh."""
from collections import Counter
from copy import deepcopy

import pytest

from packages.domain import candidate_evidence as ce, fairness, finding_causes, score, validation as v
from packages.domain.time import aware
from tests.test_schedule_score import data, shift


def test_absence_evidence_reuses_complete_reasons_without_mutable_aliases(monkeypatch):
    session = shift('s', 5, [])
    source = data([session])
    source['policies'] = []
    source['rotations'] = [{'id': 'away', 'person_id': 'a', 'department_available': False,
                            'starts_on': '2026-10-01', 'ends_on': '2026-11-01'}]
    source['availability'] = [{'id': 'leave', 'person_id': 'a', 'start': session['start'], 'end': session['end']}]
    expected = ce.candidate_evidence(source, 'a', session)
    original = deepcopy(source)
    calls, canonical = Counter(), ce.person_rows

    def counted(context, name, pid):
        calls[name] += 1
        return canonical(context, name, pid)

    monkeypatch.setattr(ce, 'person_rows', counted)
    view = {**source, '_candidate_absence_cache': {}}
    for _ in range(3):
        actual = ce.candidate_evidence(view, 'a', session)
        assert actual == expected
        actual['exclusions'][0]['code'] = 'must-not-retain'
        actual['exclusions'].append({'code': 'must-not-retain'})
    assert calls['availability'] == calls['rotations'] == 1
    assert [r['code'] for r in expected['exclusions']] == ['EXTERNAL_ROTATION', 'UNAVAILABLE']
    assert source == original


@pytest.mark.parametrize('dimension', ['person', 'start', 'end'])
def test_absence_preparation_keeps_person_and_exact_interval_keys(monkeypatch, dimension):
    session = shift('s', 5, [])
    source = data([session])
    source['policies'] = []
    source['availability'] = [{'id': 'leave', 'person_id': 'a', 'start': session['start'], 'end': session['end']}]
    view = {**source, '_candidate_absence_cache': {}}
    ce.candidate_evidence(view, 'a', session)
    pid, changed = 'a', deepcopy(session)
    if dimension == 'person':
        pid = 'b'
    elif dimension == 'start':
        changed['start'] = '2026-10-05T09:00:00+00:00'
    else:
        changed['end'] = '2026-10-05T15:00:00+00:00'
    assert ce.candidate_evidence(view, pid, changed) == ce.candidate_evidence(source, pid, changed)
    assert len(view['_candidate_absence_cache']) == 2


@pytest.mark.parametrize('malformed', ['date', 'record-id'])
def test_absence_errors_propagate_and_do_not_publish_partial_preparation(malformed):
    session = shift('s', 5, [])
    source = data([session])
    source['policies'] = []
    source['rotations'] = [{'id': 'away', 'person_id': 'a', 'department_available': False,
                            'starts_on': '2026-10-01', 'ends_on': None}]
    row = {'id': 'leave', 'person_id': 'a', 'start': session['start'], 'end': session['end']}
    source['availability'] = [row]
    if malformed == 'date':
        row['start'] = 'malformed'
    else:
        row.pop('id')
    view = {**source, '_candidate_absence_cache': {}}
    error = ValueError if malformed == 'date' else KeyError
    with pytest.raises(error) as original:
        ce.candidate_evidence(source, 'a', session)
    with pytest.raises(error) as actual:
        ce.candidate_evidence(view, 'a', session)
    assert actual.value.args == original.value.args
    assert view['_candidate_absence_cache'] == {}
    row.update(id='leave', start=session['start'])
    assert ce.candidate_evidence(view, 'a', session) == ce.candidate_evidence(source, 'a', session)


def test_audience_prepares_clocks_without_changing_candidate_probes(monkeypatch):
    sessions = [shift('one', 5, []), shift('two', 6, [])]
    source = data(sessions)
    source['policies'] = []
    original = deepcopy(source)
    clocks, probes, canonical = [], [], fairness.aware

    def clock(value):
        clocks.append(value)
        return canonical(value)

    def candidate(context, pid, session):
        probes.append((pid, session['id']))
        return False

    monkeypatch.setattr(fairness, 'aware', clock)
    monkeypatch.setattr(ce, 'candidate_eligible', candidate)
    assert fairness.eligible_people(source, None, list(source['people'])) == []
    assert len(clocks) == 2 * len(sessions)
    assert probes == [(pid, session['id']) for pid in source['people'] for session in sessions]
    assert source == original


def reference_preparation(monkeypatch):
    categories, capabilities, absences = v.categories_for, v.capabilities_for, ce._absence_exclusions
    init = finding_causes.FindingCauses.__init__

    def category(context, *args):
        return categories({**context, '_categories_cache': None}, *args)

    def capability(context, *args):
        return capabilities({**context, '_capabilities_cache': None}, *args)

    def absence(context, *args):
        return absences({**context, '_candidate_absence_cache': None}, *args)

    class RecomputedRoles(dict):
        def __init__(self, causes):
            self.causes = causes

        def __getitem__(self, key):
            sid, a, b = key
            causes = self.causes
            return {pid: v.participation_for(causes.data, pid, causes.shifts[sid], a, b)
                    for pid in causes.data['people']}

    def uncached_init(self, source):
        init(self, source)
        self.coverage_roles = RecomputedRoles(self)

    monkeypatch.setattr(v, 'categories_for', category)
    monkeypatch.setattr(v, 'capabilities_for', capability)
    monkeypatch.setattr(ce, '_absence_exclusions', absence)
    monkeypatch.setattr(finding_causes.FindingCauses, '__init__', uncached_init)


@pytest.mark.parametrize('explain_findings', [False, True])
def test_twenty_immutable_trials_match_complete_uncached_validation_goals_and_score(h, monkeypatch, explain_findings):
    from tests.test_solver_supervision_cache import assignment, snapshot

    source, _, _, _, independent, trainee, supervisor, other = snapshot(h)
    original = deepcopy(source)
    views = [source]
    for n in range(20):
        person = (supervisor, trainee, other)[n % 3]
        changed = {**independent, 'participants': [assignment(independent, person, id=f'fixed-prep-{n}')]}
        views.append({**source, 'shifts': [changed if row is independent else row for row in source['shifts']]})
    cache, counts, mode = {}, Counter(), 'actual'
    person_rows = v.person_rows

    def rows(context, name, pid):
        if name in ('memberships', 'grants', 'rotations', 'availability'):
            counts[mode, name] += 1
        return person_rows(context, name, pid)

    monkeypatch.setattr(v, 'person_rows', rows)
    monkeypatch.setattr(ce, 'person_rows', rows)
    for view in views:
        mode = 'reference'
        with monkeypatch.context() as patch:
            reference_preparation(patch)
            expected = v.evaluate(view, explain_findings=explain_findings)
            expected_score = score.schedule_score(view, expected)
        mode = 'actual'
        actual = v.evaluate(view, session_cache=cache, explain_findings=explain_findings)
        assert actual == expected
        assert score.schedule_score(view, actual) == expected_score
    assert sum(counts['actual', n] for n in ('memberships', 'grants')) < sum(
        counts['reference', n] for n in ('memberships', 'grants'))
    assert sum(counts['actual', n] for n in ('rotations', 'availability')) < sum(
        counts['reference', n] for n in ('rotations', 'availability'))
    assert source == original


@pytest.mark.parametrize('edit', ['active', 'membership', 'category-date', 'eligibility', 'grant',
                                  'leave', 'rotation', 'reservation', 'participation', 'focus', 'where'])
def test_new_evaluation_refreshes_every_fact_and_preserves_full_explanations(h, monkeypatch, edit):
    from tests.test_solver_supervision_cache import assignment, snapshot

    source, first, _, _, independent, trainee, supervisor, _ = snapshot(h)
    v.evaluate(source)
    if edit == 'active':
        source['people'][trainee]['active'] = False
    elif edit == 'membership':
        source['memberships'] = [r for r in source['memberships'] if r['person_id'] != trainee]
    elif edit == 'category-date':
        category = next(r['category_id'] for r in source['memberships'] if r['person_id'] == trainee)
        source['categories'][category]['ends_on'] = '2026-10-04'
    elif edit == 'eligibility':
        source['eligibilities'] = [r for r in source['eligibilities'] if r['person_id'] != trainee]
    elif edit == 'grant':
        source['grants'] = [r for r in source['grants'] if r['person_id'] != supervisor]
    elif edit == 'leave':
        source['availability'].append({'id': 'leave', 'person_id': trainee,
                                      'start': first['start'], 'end': first['end'], 'kind': 'recorded_leave'})
    elif edit == 'rotation':
        source.setdefault('rotations', []).append({'id': 'away', 'person_id': trainee,
            'department_available': False, 'starts_on': '2020-01-01', 'ends_on': None})
    elif edit == 'reservation':
        independent['participants'] = [assignment(independent, supervisor)]
    elif edit == 'participation':
        next(p for p in first['participants'] if p['person_id'] == trainee)['supervisor_person_id'] = None
    elif edit == 'focus':
        source['focus_ids'][:] = [first['id']]
    else:
        first['location_id'] = independent['location_id']
    original = deepcopy(source)
    for explain in (False, True):
        actual = v.evaluate(source, explain_findings=explain)
        with monkeypatch.context() as patch:
            reference_preparation(patch)
            expected = v.evaluate(source, explain_findings=explain)
            expected_score = score.schedule_score(source, expected)
        assert actual == expected and score.schedule_score(source, actual) == expected_score
    assert source == original


def test_coverage_reuses_roles_but_keeps_the_complete_hypothetical_pool(h, monkeypatch):
    from tests.test_solver_supervision_cache import snapshot

    source, first, *_ = snapshot(h)
    a, b = aware(first['start']), aware(first['end'])
    counts, pools, mode = Counter(), [], 'actual'
    role, cover = v.participation_for, v.rule_coverage_people

    def counted(*args):
        counts[mode] += 1
        return role(*args)

    def pool(context, peer, *args, **kwargs):
        pools.append([p['person_id'] for p in peer['participants']])
        return cover(context, peer, *args, **kwargs)

    monkeypatch.setattr(v, 'participation_for', counted)
    monkeypatch.setattr(v, 'rule_coverage_people', pool)
    causes = finding_causes.FindingCauses(source)
    actual = causes.coverage('SUPERVISION_GAP', first['id'], a, b, {'trainees': 1})
    assert causes.coverage('SUPERVISION_GAP', first['id'], a, b, {'trainees': 1}) == actual
    assert counts['actual'] == len(source['people'])
    assert pools == [list(source['people'])]
    mode = 'reference'
    with monkeypatch.context() as patch:
        reference_preparation(patch)
        expected = finding_causes.FindingCauses(source).coverage(
            'SUPERVISION_GAP', first['id'], a, b, {'trainees': 1})
    assert actual == expected and counts['reference'] > counts['actual']


@pytest.mark.parametrize('mode', ['disabled', 'explicit'])
def test_evaluate_preserves_explicit_authority_contract_and_owns_absence_preparation(h, monkeypatch, mode):
    from tests.test_solver_supervision_cache import snapshot

    source, *_ = snapshot(h)
    source.update(_categories_cache=None if mode == 'disabled' else {},
                  _capabilities_cache=None if mode == 'disabled' else {},
                  _candidate_absence_cache={'sentinel': ()})
    supplied = {k: source[k] for k in ('_categories_cache', '_capabilities_cache')}
    public = deepcopy({k: value for k, value in source.items() if k not in supplied})
    actual = v.evaluate(source)
    with monkeypatch.context() as patch:
        reference_preparation(patch)
        assert v.evaluate(source) == actual
    assert all(source[k] is value for k, value in supplied.items())
    assert {k: value for k, value in source.items() if k not in supplied} == public
