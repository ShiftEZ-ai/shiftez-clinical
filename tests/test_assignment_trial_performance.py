"""Immutable request preparation and exact, bounded placement-score trials."""
from collections import Counter
import copy
from datetime import timedelta

from packages.domain import score, validation
from tests.test_schedule_score_scale import department


def test_department_placement_trials_prepare_once_and_skip_duplicate_people(monkeypatch):
    data = department()
    original = copy.deepcopy(data)
    target = data['shifts'][0]
    people = list(data['people'])[:3]
    checked = validation.evaluate(data, explain_findings=False)
    expected = {}
    base = score.schedule_score(data, checked, limit=0)
    for pid in people:
        on = any(p['person_id'] == pid for p in target['participants'])
        participants = ([p for p in target['participants'] if p['person_id'] != pid] if on else
                        target['participants'] + [{'id': 'why-' + pid, 'shift_id': target['id'], 'person_id': pid,
                            'start': target['start'], 'end': target['end'], 'participation': 'staff', 'overtime': False}])
        trial = {**data, 'shifts': [dict(s, participants=participants) if s is target else s for s in data['shifts']]}
        after = score.schedule_score(trial, validation.evaluate(trial, explain_findings=False), limit=0)
        expected[pid] = {**(score.score_effect(base, after) if on else score.score_effect(after, base)), 'wishes': []}

    calls = Counter()
    for module, name in ((validation, 'active_rules'), (score, 'active_rules'),
                         (validation, 'evaluate'), (validation, 'validate_session_setup')):
        real = getattr(module, name)
        def counted(*args, _real=real, _key=(module.__name__, name), **kwargs):
            calls[_key] += 1
            return _real(*args, **kwargs)
        monkeypatch.setattr(module, name, counted)
    actual = score.placement_effects(data, checked, target['id'], people * 2, base_score=base)
    assert actual == expected
    assert data == original
    assert calls[validation.__name__, 'evaluate'] == len(people)
    assert calls[validation.__name__, 'active_rules'] == 1
    assert calls[score.__name__, 'active_rules'] == 0
    assert calls[validation.__name__, 'validate_session_setup'] <= len(data['shifts']) + len(people) - 1


def test_prepared_trials_match_full_checks_and_rebuild_after_fact_edits():
    data = department()
    prepared = validation.prepare_assignment_trials(data)
    target = data['shifts'][0]
    # A removal changes cover, workload and fairness. Dated inputs retain their exact meaning.
    shifts = [dict(s, participants=[]) if s is target else s for s in data['shifts']]
    trial = {**prepared, 'shifts': shifts}
    raw = {**data, 'shifts': shifts}
    checked = validation.evaluate(raw, explain_findings=False)
    assert validation.evaluate(trial, explain_findings=False) == checked
    assert score.schedule_score(trial, checked) == score.schedule_score(raw, checked)
    changed = copy.deepcopy(data)
    changed['policies'][0]['active'] = False
    updated = validation.prepare_assignment_trials(changed)
    assert validation.evaluate(updated) == validation.evaluate(changed)


def test_staffing_keeps_full_policy_view_when_original_rules_have_overrides():
    from apps.api.services.staffing import people_index
    data = department()
    data['policies'][0]['definition']['overrides'] = [data['policies'][1]['id']]
    assert people_index(data)['personal'] is None


def test_prepared_trials_recompute_weekly_staffing_selection_for_reduced_shift_views(h):
    from packages.domain.time import aware
    from tests.test_source_parity_selection import world
    data = world(h, period='week')
    first, second, _ = h.seed['people']
    shift = data['shifts'][0]
    peer = copy.deepcopy(shift)
    peer.update(id=shift['id'] + '-next-day', start=(aware(shift['start']) + timedelta(days=1)).isoformat(),
                end=(aware(shift['end']) + timedelta(days=1)).isoformat())
    for row, person_id in ((shift, first), (peer, second)):
        row['participants'] = [{'id': 'selection-' + person_id, 'shift_id': row['id'], 'person_id': person_id,
                                'start': row['start'], 'end': row['end'], 'participation': 'staff', 'overtime': False}]
    data['shifts'].append(peer)
    data['focus_ids'].append(peer['id'])
    data['availability'].append({'id': 'selection-absence', 'person_id': first, 'kind': 'recorded_leave',
                                  'start': peer['start'], 'end': peer['end']})
    reduced = {**data, 'shifts': [peer], 'focus_ids': [peer['id']]}
    expected = validation.evaluate(reduced)
    assert not [f for f in expected['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP']
    prepared = validation.prepare_assignment_trials(data)
    full = validation.evaluate(prepared)
    assert [f for f in full['findings'] if f['code'] == 'SCOPED_COVERAGE_GAP' and f['shift_id'] == peer['id']]
    assert validation.evaluate({**prepared, 'shifts': [peer], 'focus_ids': [peer['id']]}) == expected
