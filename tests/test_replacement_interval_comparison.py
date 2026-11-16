"""Replacement checks compare every mandatory constraint throughout its observed intervals."""
import copy

import pytest

from apps.api.services.issues import issue_key
from packages.domain.feasibility import compare_findings
from packages.domain.time import aware
from tests.test_resolve_issues import issues, preview, resolve, resolution_history


def gap(start=8, end=12, *, code='COVERAGE_GAP', detail=None, **changes):
    return {'code': code, 'severity': 'error', 'shift_id': 'duty', 'person_id': None, 'rule_id': None,
            'start': f'2026-10-04T{start:02d}:00:00+03:00',
            'end': f'2026-10-04T{end:02d}:00:00+03:00',
            'detail': {'required': 2, 'available': 1} if detail is None else detail, **changes}


def phase(findings):
    return {'status': 'requirements_conflict', 'findings': findings, 'diagnostics': [], 'candidate_evidence': []}


@pytest.mark.parametrize('code,before,after', [
    ('COVERAGE_GAP', {'required': 2, 'available': 0}, {'required': 2, 'available': 1}),
    ('CAPABILITY_GAP', {'required': 2, 'available': 0, 'capability_id': 'skill'},
                       {'required': 2, 'available': 1, 'capability_id': 'skill'}),
    ('SUPERVISION_GAP', {'trainees': 3, 'qualified_supervisors': 0},
                        {'trainees': 2, 'qualified_supervisors': 1}),
    ('SUPERVISOR_SCOPED_CAPACITY', {'maximum': 1, 'scheduled': 3}, {'maximum': 1, 'scheduled': 2}),
])
@pytest.mark.parametrize('merged', [False, True])
def test_counted_interval_improvement_survives_splitting_or_merging(code, before, after, merged):
    original = [gap(8, 9, code=code, detail=before), gap(9, 10, code=code, detail=before),
                gap(10, 12, code=code, detail=before)] if merged else [gap(code=code, detail=before)]
    proposed = [gap(code=code, detail=after)] if merged else [
        gap(8, 9, code=code, detail=after), gap(9, 12, code=code, detail=after)]
    baseline = copy.deepcopy(original)

    result = compare_findings(phase(original), phase(proposed))

    assert result['no_new_problems'] and result['comparison_complete']
    assert result['new_or_worse_findings'] == []
    assert result['unchanged_existing_count'] == len(proposed)
    assert original == baseline


@pytest.mark.parametrize('available_before,available_after,blocked', [(0, 1, False), (1, 1, False), (1, 0, True)])
@pytest.mark.parametrize('merged', [False, True])
def test_staffing_compares_supervision_canonical_measures_without_losing_raw_evidence(
        available_before, available_after, blocked, merged):
    from apps.api.services.staffing import _new_candidate_findings, candidate_status

    before = {'trainees': 2, 'qualified_supervisors': available_before, 'required': 2, 'available': available_before}
    after = {'trainees': 2, 'qualified_supervisors': available_after, 'required': 2, 'available': available_after}
    baseline = ([gap(8, 9, code='SUPERVISION_GAP', detail=before), gap(9, 12, code='SUPERVISION_GAP', detail=before)]
                if merged else [gap(code='SUPERVISION_GAP', detail=before)])
    proposed = ([gap(code='SUPERVISION_GAP', detail=after)] if merged else
                [gap(8, 9, code='SUPERVISION_GAP', detail=after), gap(9, 12, code='SUPERVISION_GAP', detail=after)])
    original = copy.deepcopy((baseline, proposed))

    findings = _new_candidate_findings(phase(baseline), phase(proposed))

    assert candidate_status(findings) == ('blocked' if blocked else 'ok')
    assert [row['detail'] for row in findings] == ([row['detail'] for row in proposed] if blocked else [])
    assert (baseline, proposed) == original


@pytest.mark.parametrize('before,proposed', [
    ([gap(8, 9), gap(10, 12)], [gap()]),  # A previously covered hour cannot become short.
    ([gap()], [gap(7, 12)]),
    ([gap(8, 9), gap(9, 12, detail={'required': 2, 'available': 0})],
     [gap(8, 9, detail={'required': 2, 'available': 0}), gap(9, 12)]),
    ([gap()], [gap(), gap()]),  # One old observation cannot hide two new constraints.
])
def test_interval_comparison_blocks_new_or_locally_worse_gaps(before, proposed):
    result = compare_findings(phase(before), phase(proposed))
    assert not result['no_new_problems']
    assert result['new_or_worse_findings']


@pytest.mark.parametrize('changes', [
    {'shift_id': 'other-duty'}, {'person_id': 'other-person'}, {'rule_id': 'other-rule'},
    {'detail': {'required': 2, 'available': 1, 'line': 'other-line'}},
    {'detail': {'required': 2, 'available': 1, 'capability_id': 'other-skill'}},
])
def test_interval_comparison_keeps_constraint_identity(changes):
    result = compare_findings(phase([gap()]), phase([gap(**changes)]))
    assert len(result['new_or_worse_findings']) == 1


def partial_day_absence(h):
    typ = h.configure('assignment_types', {'name': 'Partial absence cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 2}]}})['record']['id']
    workspace = h.shift(h.workspace('Partial absence replacement'), typ=typ)['workspace']
    h.command('coordinator', 'availability.record', {'person_id': h.seed['people'][0],
        'kind': 'recorded_leave', 'leave_type': 'Annual leave', 'start': gap(9, 10)['start'],
        'end': gap(9, 10)['end'], 'private_reason': 'Synthetic absence'})
    workspace = h.lifecycle(h.detail(workspace)['workspace'], 'validate')['workspace']
    keys = [issue_key(f) for f in issues(h, workspace, 'UNAVAILABLE')]
    assert len(keys) == 1
    return workspace, keys


def test_partial_day_absence_replacement_can_improve_incomplete_cover(h):
    workspace, keys = partial_day_absence(h)
    before = issues(h, workspace, 'COVERAGE_GAP')
    assert [f['detail']['available'] for f in before] == [1, 0, 1]
    plan = preview(h, workspace, keys)
    assert plan['replaceable'] == 1

    done = resolve(h, workspace, 'replace', keys, plan['plan_hash'])

    assert not issues(h, done['workspace'], 'UNAVAILABLE')
    remaining = issues(h, done['workspace'], 'COVERAGE_GAP')
    assert len(remaining) == 1
    assert (aware(remaining[0]['start']), aware(remaining[0]['end'])) == (aware(gap()['start']), aware(gap()['end']))
    assert remaining[0]['detail']['required'] == 2 and remaining[0]['detail']['available'] == 1


def test_partial_day_replacement_still_blocks_a_new_mandatory_finding_on_an_untouched_session(h, monkeypatch):
    from apps.api.services import issues as service

    workspace, keys = partial_day_absence(h)
    replacement = h.seed['people'][1]
    rest = h.policy({'kind': 'min_rest', 'hours': 24}, {'person_id': replacement})
    later = h.shift(workspace, '2026-10-05T08:00:00+03:00', '2026-10-05T12:00:00+03:00',
                    people=[replacement])
    workspace = later['workspace']
    later_id = later['shifts'][0]['id']
    best = service.best_replacements

    def unsafe_plan(*args, **kwargs):
        rows = best(*args, **kwargs)
        rows[0].update(replacement_id=replacement, replacement_name='Synthetic blocked replacement', status='ok', findings=[])
        return rows

    monkeypatch.setattr(service, 'best_replacements', unsafe_plan)
    plan = preview(h, workspace, keys)
    before, history = h.detail(workspace), resolution_history(h, workspace)
    compared, findings = service.compare_findings, []

    def checked(baseline, proposed, *, protect_intervals):
        assert protect_intervals is True
        result = compared(baseline, proposed, protect_intervals=protect_intervals)
        findings.extend(result['new_or_worse_findings'])
        return result

    monkeypatch.setattr(service, 'compare_findings', checked)
    refused = resolve(h, workspace, 'replace', keys, plan['plan_hash'], status=409)

    assert refused['error']['code'] == 'REPLACEMENT_BLOCKED'
    assert any(f['code'] == 'MIN_REST' and f['shift_id'] == later_id and f['rule_id'] == rest['id'] for f in findings)
    assert h.detail(workspace) == before and resolution_history(h, workspace) == history
