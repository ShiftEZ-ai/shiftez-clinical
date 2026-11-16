"""Fixed-rule interval protection counts citations; rule effects compare substance."""
from copy import deepcopy

import pytest

from packages.domain import feasibility
from packages.domain.feasibility import compare_findings
from packages.domain.rule_effects import observed_effect
from .test_finding_identity import preview
from .test_replacement_interval_comparison import gap, phase


def test_planning_apply_guard_keeps_equal_duplicate_constraint_multiplicity(monkeypatch):
    from apps.api.services import planning
    from packages.domain import validation_scope

    finding = gap(code='MAXIMUM_PARTICIPANTS', rule_id='same-rule',
                  detail={'maximum': 1, 'scheduled': 2, 'line': 0, 'line_id': 'same-line'})
    shift = {'id': 'duty', 'start': finding['start'], 'end': finding['end'], 'participants': []}
    data = {'organisation': {'timezone': 'Asia/Riyadh'}, 'focus_ids': ['duty'],
            'shifts': [{**shift, 'participants': [{'id': 'added', 'person_id': 'person'}]}]}
    check = {'valid': False, 'findings': [finding, deepcopy(finding)]}
    comparisons = []
    monkeypatch.setattr(planning, 'domain_snapshot', lambda *a, **kw: data)
    monkeypatch.setattr(validation_scope, 'evaluate_assignments', lambda *a, **kw: deepcopy(check))

    def compute(db, kind, source, operation, **kwargs):
        result = operation()
        comparisons.append(result[0])
        return result

    monkeypatch.setattr(planning, 'command_compute', compute)
    checked, _ = planning.admit_assignment_change(None, None, {'shifts': [shift]}, 'EDIT_INVALID')

    assert checked == check
    [comparison] = comparisons
    assert comparison['no_new_problems'] and comparison['unchanged_existing_count'] == 2
    assert comparison['new_or_worse_findings'] == []


@pytest.mark.parametrize('grouped', [False, True])
def test_replacement_counts_same_substance_from_distinct_rules(grouped):
    first, second = gap(rule_id='first-rule'), gap(rule_id='second-rule')
    findings = [first, second]
    if grouped:
        findings = [gap(rule_id='first-rule', detail={**first['detail'], 'derived_findings': [second]})]

    retained = compare_findings(phase(findings), phase([first, second]))
    assert retained['no_new_problems'] and retained['unchanged_existing_count'] == 2

    added = compare_findings(phase([first]), phase(findings))
    assert not added['no_new_problems'] and added['unchanged_existing_count'] == 1
    assert [f['rule_id'] for f in added['new_or_worse_findings']] == ['second-rule']


@pytest.mark.parametrize('aggregate', [False, True])
def test_interval_constraint_multiplicity_survives_normalization(aggregate):
    finding = gap(code='SCOPED_COVERAGE_GAP', rule_id='cover-rule')
    result = compare_findings(phase([finding, finding]), phase([finding, finding, finding]),
                              aggregate_coverage=aggregate)
    assert not result['no_new_problems'] and result['unchanged_existing_count'] == 2
    assert len(result['new_or_worse_findings']) == 1


@pytest.mark.parametrize('citation', [{'rule_id': 'other-rule'},
    {'detail': {'required': 2, 'available': 1, 'line': 1, 'line_id': 'other-line'}}])
def test_interval_recitation_keeps_impacts_and_effects_unchanged(monkeypatch, citation):
    before = gap(rule_id='cover-rule')
    after = {**before, **citation}
    assert not compare_findings(phase([before]), phase([after]))['no_new_problems']

    impact = preview(monkeypatch, [before], [after])
    assert impact['introduced'] == impact['resolved'] == []
    assert len(impact['recited']) == 1
    effect = observed_effect(phase([before]), phase([after]))
    assert effect['adds'] == effect['clears'] == 0 and effect['summary'] == 'No change'
    assert effect['recited'] == impact['recited']


def test_rule_change_check_keeps_interval_recitation_as_substance(monkeypatch):
    before, after = gap(rule_id='before-rule'), gap(rule_id='after-rule')
    baseline = {'workspace': {'status': 'draft'}, 'shifts': [], 'people': {}, 'policies': []}
    proposed = {**deepcopy(baseline), 'policies': [{'id': 'after-rule'}]}
    monkeypatch.setattr(feasibility, 'generate', lambda *a, **kw: {
        'status': 'candidate_feasible', 'candidate_evidence': []})
    monkeypatch.setattr(feasibility, 'evaluate', lambda data: {
        'findings': [after if data['policies'] else before]})

    result = feasibility.check({'baseline': baseline, 'proposed': proposed, 'bindings': [],
                                'fingerprint': 'synthetic', 'check_seconds': 1})
    assert result['no_new_problems'] and result['unchanged_existing_count'] == 1
    assert len(result['recited']) == 1 and result['effect']['adds'] == result['effect']['clears'] == 0
