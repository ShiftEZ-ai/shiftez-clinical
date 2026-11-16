"""Assignment-only evidence reuse preserves the old complete replacement calculation."""
from collections import Counter
from copy import deepcopy

import pytest

from apps.api.services import issues, staffing
from packages.domain import leadership, validation
from packages.domain import quotas


def test_500_replacements_equal_the_old_uncached_complete_checks(scale_harness, monkeypatch):
    from tests.test_resolve_issues import department_rota

    data, targets, people = department_rota(scale_harness('h'))
    original = deepcopy(data)
    actual = issues.best_replacements(data, targets, people)
    canonical_evaluate, canonical_plan = staffing.evaluate, staffing.supervision_plan
    canonical_index, canonical_move = issues.people_index, issues.move_seat
    raw_sources = {}

    def legacy_index(source):
        index = canonical_index(source)
        raw_sources[id(index['fast'])] = source
        return index

    def legacy_evaluate(source, **kwargs):
        kwargs.pop('trial_cache', None)
        return canonical_evaluate(source, **kwargs)

    def legacy_plan(*args, **kwargs):
        kwargs['require_proposal'] = False
        return canonical_plan(*args, **kwargs)

    def legacy_move(index, *args, **kwargs):
        canonical_move(index, *args, **kwargs)
        index.pop('supervision_session_cache', None)

    # Restore the old raw-snapshot reads, complete trainee verdicts and per-move cache disposal.
    # The canonical uncached rule/attribution bodies remain the independent behavioural oracle.
    for module, name in ((leadership, 'qualified'), (validation, 'qualification_failures'),
                         (validation, 'categories_for')):
        canonical = getattr(module, name)
        def legacy_facts(source, *args, _canonical=canonical, **kwargs):
            return _canonical(raw_sources.get(id(source), source), *args, **kwargs)
        monkeypatch.setattr(module, name, legacy_facts)
    monkeypatch.setattr(issues, 'people_index', legacy_index)
    monkeypatch.setattr(issues, 'move_seat', legacy_move)
    monkeypatch.setattr(staffing, 'evaluate', legacy_evaluate)
    monkeypatch.setattr(staffing, 'supervision_plan', legacy_plan)
    assert issues.best_replacements(data, targets, people) == actual
    assert len(actual) == len(targets) == 500
    assert data == original


@pytest.mark.parametrize('edit', ['replace', 'overtime', 'supervisor', 'segments', 'focus', 'remove'])
def test_trial_cache_refreshes_changed_people_and_keeps_complete_reports(h, monkeypatch, edit):
    a, b, c = h.seed['people']
    h.policy({'kind': 'max_duties', 'maximum': 1, 'window': 'week'})
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
              'metric': 'duties', 'comparison': 'maximum', 'amount': 1})
    workspace = h.workspace()
    for pid, day in ((a, '04'), (a, '05'), (b, '06'), (b, '07')):
        workspace = h.shift(workspace, f'2026-10-{day}T08:00:00+03:00',
                            f'2026-10-{day}T12:00:00+03:00', people=[pid])['workspace']
    data = h.snapshot(workspace)
    original = deepcopy(data)
    data = validation.prepare_assignment_trials(data)
    cache, calls = {}, Counter()
    workload, attribution = validation.workload_selected, quotas.quota_contribution

    def own(context, policy, pid, *args):
        calls['rule', pid] += 1
        return workload(context, policy, pid, *args)

    def quota(context, policy, pid, *args):
        calls['quota', pid] += 1
        return attribution(context, policy, pid, *args)

    monkeypatch.setattr(validation, 'workload_selected', own)
    monkeypatch.setattr(quotas, 'quota_contribution', quota)
    expected = validation.evaluate(data)
    assert validation.evaluate(data, trial_cache=cache) == expected
    # Explanatory cause builders intentionally refresh their independent attribution; decision trials omit them.
    plain = validation.evaluate(data, explain_findings=False)
    calls.clear()
    assert validation.evaluate(data, trial_cache=cache, explain_findings=False) == plain
    assert not calls, 'Unchanged personal rules and quota contributions must reuse their existing evidence'

    target = data['shifts'][0]
    seat = target['participants'][0]
    changed_people = {seat['person_id']}
    if edit == 'replace':
        seat['person_id'] = c
        changed_people.add(c)
    elif edit == 'overtime':
        seat['overtime'] = True
    elif edit == 'supervisor':
        seat['supervisor_person_id'] = b
    elif edit == 'segments':
        seat['segments'] = [{'start': seat['start'], 'end': seat['end']}]
    elif edit == 'focus':
        data['focus_ids'] = [target['id']]
        changed_people.add(b)
    else:
        target['participants'].clear()
    expected = validation.evaluate(data)
    plain = validation.evaluate(data, explain_findings=False)
    calls.clear()
    assert validation.evaluate(data, trial_cache=cache, explain_findings=False) == plain
    assert {pid for _, pid in calls} <= changed_people
    assert calls, 'Changed duties/focus must not retain their old personal verdict'
    assert validation.evaluate(data, trial_cache=cache) == expected
    # Cache state stays private; rebuilding after a recorded fact edit also matches a fresh complete check.
    original['availability'].append({'id': 'new-absence', 'person_id': a, 'kind': 'recorded_leave',
                                      'start': target['start'], 'end': target['end']})
    fresh = validation.prepare_assignment_trials(original)
    expected = validation.evaluate(original)
    assert any(f['code'] == 'UNAVAILABLE' for f in expected['findings'])
    assert validation.evaluate(fresh, trial_cache={}) == expected


@pytest.mark.parametrize('alignment', ['rolling', 'fixed', 'calendar_week'])
def test_expanded_quota_trial_evidence_is_bounded_by_windows_and_people(h, alignment):
    h.policy({'kind': 'block_quota', 'starts_on': '2026-10-04', 'ends_on': '2026-10-18',
              'window_alignment': alignment, 'window_days': 7 if alignment != 'calendar_week' else None,
              'metric': 'duties', 'comparison': 'maximum', 'amount': 1})
    workspace = h.shift(h.workspace())['workspace']
    workspace = h.shift(workspace, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00')['workspace']
    data = validation.prepare_assignment_trials(h.snapshot(workspace))
    cache = {}
    expected = validation.evaluate(data)
    assert validation.evaluate(data, trial_cache=cache) == expected
    entries = set(cache['quota_contributions'])
    assert entries and len(entries) <= len(cache['quota_policies']) * len(data['people'])
    assert validation.evaluate(data, trial_cache=cache) == expected
    assert set(cache['quota_contributions']) == entries


def test_replayed_person_findings_keep_canonical_duplicate_suppression(h):
    h.policy({'kind': 'max_duties', 'maximum': 0, 'window': 'week'})
    data = validation.prepare_assignment_trials(h.snapshot(h.shift(h.workspace())['workspace']))
    data['_counted_policies'] *= 2
    cache = {}
    expected = validation.evaluate(data)
    assert validation.evaluate(data, trial_cache=cache) == expected
    assert validation.evaluate(data, trial_cache=cache) == expected
