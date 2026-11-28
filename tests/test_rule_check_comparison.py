"""Activation compares source-linked problems, not overall rota feasibility."""
import copy

import pytest

from packages.domain.feasibility import compare_findings


START = '2026-10-04T08:00:00+03:00'
END = '2026-10-04T16:00:00+03:00'


def finding(**changes):
    return {'code': 'MIN_REST', 'severity': 'error', 'shift_id': 'duty',
            'person_id': 'person', 'rule_id': 'rest-rule', 'start': START, 'end': END,
            'detail': {'required_hours': 24, 'actual_hours': 16, 'rule_severity': 'mandatory'},
            'message': 'Requires 24 hours of rest', **changes}


def shortage(**changes):
    return {'code': 'INSUFFICIENT_CAPABILITY_CANDIDATES', 'proof_kind': 'necessary_condition',
            'proven': True, 'shift_id': 'duty', 'start': START, 'end': END,
            'capability_id': 'capability', 'requirement_path': 'definition.requirements.0.capabilities.0.minimum',
            'required': 3, 'available_upper_bound': 1, 'checked_at': START,
            'input_fingerprint': 'baseline', 'message': 'Needs two more people', **changes}


def candidates():
    return [{'shift_id': 'duty', 'person_id': person, 'start': START, 'end': END,
             'eligible_and_available': True, 'exclusions': []}
            for person in ('person', 'second-person', 'third-person')]


def phase(*, findings=(), diagnostics=(), status='requirements_conflict'):
    return {'status': status, 'findings': copy.deepcopy(list(findings)),
            'diagnostics': copy.deepcopy(list(diagnostics)), 'candidate_evidence': candidates()}


def test_fact_only_check_keeps_solver_receipt_without_rule_comparison(monkeypatch):
    from packages.domain import feasibility
    data = {'workspace': {'status': 'draft'}, 'shifts': [], 'focus_ids': [], 'people': {}, 'policies': []}
    monkeypatch.setattr(feasibility, 'generate', lambda *args, **kwargs: {'status': 'candidate_feasible',
        'validation': {'valid': True}})
    def no_rule_comparison(*args, **kwargs):
        pytest.fail('A fact-only receipt must not add rule comparison or full validation passes')
    monkeypatch.setattr(feasibility, 'evaluate', no_rule_comparison)
    monkeypatch.setattr(feasibility, 'compare_findings', no_rule_comparison)

    result = feasibility.check({'baseline': data, 'proposed': copy.deepcopy(data),
        'bindings': [{'policy_id': 'fact', 'fact_id': 'fact'}], 'fingerprint': 'same-inputs', 'check_seconds': 1})

    assert result['full_feasibility_established']
    assert all(row['validation']['valid'] for row in result['checks'].values())
    assert not result['comparison_complete'] and not result['no_new_problems']


def test_mixed_rule_and_fact_check_still_compares_both_phases(monkeypatch):
    from packages.domain import feasibility
    data = {'workspace': {'status': 'draft'}, 'shifts': [], 'focus_ids': [], 'people': {}, 'policies': []}
    monkeypatch.setattr(feasibility, 'generate', lambda *args, **kwargs: {'status': 'candidate_feasible',
        'candidate_evidence': []})
    monkeypatch.setattr(feasibility, 'evaluate', lambda data: {'findings': []})

    result = feasibility.check({'baseline': data, 'proposed': copy.deepcopy(data),
        'bindings': [{'policy_id': 'fact', 'fact_id': 'fact'}, {'policy_id': 'rule'}],
        'fingerprint': 'same-inputs', 'check_seconds': 1})

    assert result['full_feasibility_established']
    assert result['comparison_complete'] and result['no_new_problems']


@pytest.mark.parametrize('known_history_start', ['2026-09-27', '2026-09-20'])
def test_rule_check_distinguishes_goal_history_from_a_changed_source(monkeypatch, known_history_start):
    from packages.domain import feasibility

    baseline = {'workspace': {'status': 'draft'}, 'shifts': [], 'focus_ids': [], 'people': {}, 'policies': [],
                'fairness_history': {}, 'fairness_history_start': '2026-09-27'}
    proposed = {**baseline, 'fairness_history': {'goal': {'people': {'person': {'value': 1}}}},
                'fairness_history_start': known_history_start}
    monkeypatch.setattr(feasibility, 'generate', lambda *args, **kwargs: {'status': 'candidate_feasible',
        'candidate_evidence': []})
    monkeypatch.setattr(feasibility, 'evaluate', lambda data: {'findings': []})

    result = feasibility.check({'baseline': baseline, 'proposed': proposed, 'bindings': [{'policy_id': 'goal'}],
                                'fingerprint': 'same-inputs', 'check_seconds': 1})

    assert result['comparison_complete'] is (known_history_start == baseline['fairness_history_start'])
    assert result['no_new_problems'] is result['comparison_complete']


def test_published_fact_conflict_keeps_required_protected_assignment_evidence(monkeypatch):
    from packages.domain import feasibility
    baseline = {'workspace': {'status': 'published'}, 'shifts': [], 'policies': []}
    proposed = {**baseline, 'policies': [{'id': 'fact', 'definition': {'rule': {'kind': 'restriction'}},
                                       'status': 'active'}]}
    monkeypatch.setattr(feasibility, 'generate', lambda data, *args, **kwargs: {
        'status': 'requirements_conflict' if data['policies'] else 'candidate_feasible'})
    finding = {'code': 'RESTRICTION', 'severity': 'error', 'rule_id': 'fact', 'shift_id': 'duty'}
    monkeypatch.setattr(feasibility, 'evaluate', lambda data: {'findings': [finding]})

    result = feasibility.check({'baseline': baseline, 'proposed': proposed,
        'bindings': [{'policy_id': 'fact', 'fact_id': 'fact'}], 'fingerprint': 'same-inputs', 'check_seconds': 1})

    assert result['newly_introduced_contradiction'] and not result['full_feasibility_established']
    assert result['protected_assignment_conflicts'] == [finding]
    assert not result['no_new_problems']


@pytest.mark.parametrize('status', ['unknown_time_limit', 'invalid_inputs', 'model_invalid'])
def test_unsuccessful_rule_check_does_not_repeat_full_validation(monkeypatch, status):
    from packages.domain import feasibility
    data = {'workspace': {'status': 'draft'}, 'shifts': [], 'focus_ids': [], 'people': {}, 'policies': []}
    monkeypatch.setattr(feasibility, 'generate', lambda *args, **kwargs: {'status': status})
    def no_full_validation(*args, **kwargs):
        pytest.fail('An unsuccessful check cannot establish comparison evidence')
    monkeypatch.setattr(feasibility, 'evaluate', no_full_validation)

    result = feasibility.check({'baseline': data, 'proposed': copy.deepcopy(data),
        'bindings': [], 'fingerprint': 'same-inputs', 'check_seconds': 1})

    assert not result['full_feasibility_established']
    assert not result['comparison_complete'] and not result['no_new_problems']


def test_unchanged_problems_ignore_display_text_receipt_metadata_and_order():
    baseline = phase(findings=[finding()], diagnostics=[shortage()])
    proposed = copy.deepcopy(baseline)
    proposed['findings'][0].update(message='Rest period remains too short', key='new-display-key')
    proposed['diagnostics'][0].update(checked_at=END, input_fingerprint='proposed',
                                      message='Existing capability shortage')
    proposed['candidate_evidence'].reverse()

    result = compare_findings(baseline, proposed)

    assert result['comparison_complete'] and result['no_new_problems']
    assert result['new_or_worse_findings'] == []
    assert result['unchanged_existing_count'] == 2


def test_added_mandatory_finding_keeps_existing_problem_count():
    baseline = phase(findings=[finding()])
    proposed = phase(findings=[finding(), finding(code='RESTRICTION', rule_id='new-rule')])

    result = compare_findings(baseline, proposed)

    assert result['comparison_complete'] and not result['no_new_problems']
    assert [row['code'] for row in result['new_or_worse_findings']] == ['RESTRICTION']
    assert result['unchanged_existing_count'] == 1


@pytest.mark.parametrize('changes', [
    {'person_id': 'second-person'}, {'shift_id': 'other-duty'},
    {'start': '2026-10-04T07:00:00+03:00'}, {'end': '2026-10-04T17:00:00+03:00'},
    {'detail': {'required_hours': 28, 'actual_hours': 16, 'rule_severity': 'mandatory'}},
])
def test_mandatory_finding_semantic_changes_are_new(changes):
    result = compare_findings(phase(findings=[finding()]), phase(findings=[finding(**changes)]))

    assert result['comparison_complete'] and not result['no_new_problems']
    assert len(result['new_or_worse_findings']) == 1
    assert result['unchanged_existing_count'] == 0


def test_advisory_warning_escalating_to_mandatory_is_new():
    advisory = finding(severity='warning', detail={'required_hours': 24, 'actual_hours': 16,
                                                 'rule_severity': 'advisory'})
    result = compare_findings(phase(findings=[advisory]), phase(findings=[finding()]))

    assert result['comparison_complete'] and not result['no_new_problems']
    assert [row['code'] for row in result['new_or_worse_findings']] == ['MIN_REST']
    assert result['unchanged_existing_count'] == 0


@pytest.mark.parametrize('changes', [{'required': 4}, {'available_upper_bound': 0},
                                    {'capability_id': 'different-capability'},
                                    {'requirement_path': 'definition.requirements.1.minimum'}])
def test_worse_or_new_shortage_requires_acknowledgement(changes):
    result = compare_findings(phase(diagnostics=[shortage()]),
                              phase(diagnostics=[shortage(**changes)]))

    assert result['comparison_complete'] and not result['no_new_problems']
    assert [row['code'] for row in result['new_or_worse_findings']] == [shortage()['code']]
    assert result['unchanged_existing_count'] == 0


def test_fewer_eligible_candidates_is_new_even_when_duty_can_still_be_staffed():
    baseline = phase(status='candidate_feasible')
    proposed = copy.deepcopy(baseline)
    proposed['candidate_evidence'][0].update(eligible_and_available=False,
        exclusions=[{'code': 'RESTRICTION', 'rule_id': 'new-rule', 'rule_revision': 1}])

    result = compare_findings(baseline, proposed)

    assert result['comparison_complete'] and not result['no_new_problems']
    assert len(result['new_or_worse_findings']) == 1
    assert result['unchanged_existing_count'] == 0


@pytest.mark.parametrize('changes', [{'required': 2}, {'available_upper_bound': 2},
                                    {'required': 2, 'available_upper_bound': 2}])
def test_improved_existing_shortage_adds_no_problem(changes):
    result = compare_findings(phase(diagnostics=[shortage()]),
                              phase(diagnostics=[shortage(**changes)]))

    assert result['comparison_complete'] and result['no_new_problems']
    assert result['new_or_worse_findings'] == []
    assert result['unchanged_existing_count'] == 1


def test_subset_of_existing_findings_adds_no_problem():
    baseline = phase(findings=[finding(), finding(code='RESTRICTION', rule_id='other-rule')])
    proposed = phase(findings=[finding()])

    result = compare_findings(baseline, proposed)

    assert result['comparison_complete'] and result['no_new_problems']
    assert result['new_or_worse_findings'] == []
    assert result['unchanged_existing_count'] == 1


def test_generation_reconciliation_aggregates_same_scoped_cover_deficit_across_sessions():
    baseline = phase(findings=[
        finding(code='SCOPED_COVERAGE_GAP', shift_id=f'old-{i}', rule_id='cover',
                start=f'2026-10-{4 + i:02d}T08:00:00+03:00', end=f'2026-10-{4 + i:02d}T12:00:00+03:00',
                detail={'required': 2, 'available': 1, 'line': 7, 'slot': 'min', 'rule_severity': 'mandatory'})
        for i in range(5)])
    proposed = phase(findings=[
        finding(code='SCOPED_COVERAGE_GAP', shift_id=f'new-{i}', rule_id='cover',
                start=f'2026-10-{14 + i:02d}T08:00:00+03:00', end=f'2026-10-{14 + i:02d}T12:00:00+03:00',
                detail={'required': 2, 'available': 1, 'line': 7, 'slot': 'min', 'rule_severity': 'mandatory'})
        for i in range(5)])
    result = compare_findings(baseline, proposed, aggregate_coverage=True)
    assert result['comparison_complete'] and result['no_new_problems']
    assert result['new_or_worse_findings'] == []

    extra = copy.deepcopy(proposed)
    extra['findings'].append(finding(code='SCOPED_COVERAGE_GAP', shift_id='new-extra', rule_id='cover',
        start='2026-10-20T08:00:00+03:00', end='2026-10-20T12:00:00+03:00',
        detail={'required': 2, 'available': 1, 'line': 7, 'slot': 'min', 'rule_severity': 'mandatory'}))
    result = compare_findings(baseline, extra, aggregate_coverage=True)
    assert not result['no_new_problems'] and len(result['new_or_worse_findings']) == 1

    deeper = copy.deepcopy(proposed)
    deeper['findings'][0]['detail']['available'] = 0
    result = compare_findings(baseline, deeper, aggregate_coverage=True)
    assert not result['no_new_problems'] and len(result['new_or_worse_findings']) == 1


@pytest.mark.parametrize('scheduled,maximum,adds', [(4, 6, False), (3, 2, True), (7, 4, True)])
def test_existing_maximum_violation_compares_directionally(scheduled, maximum, adds):
    baseline = finding(code='MAXIMUM_PARTICIPANTS', detail={'maximum': 4, 'scheduled': 5})
    proposed = finding(code='MAXIMUM_PARTICIPANTS', detail={'maximum': maximum, 'scheduled': scheduled})
    result = compare_findings(phase(findings=[baseline]), phase(findings=[proposed]))
    assert result['no_new_problems'] is not adds
    assert len(result['new_or_worse_findings']) == int(adds)


@pytest.mark.parametrize('code,comparison,before,after,adds', [
    ('QUOTA_BELOW_MINIMUM', 'minimum', 0, 1, False),
    ('QUOTA_BELOW_MINIMUM', 'minimum', 1, 0, True),
    ('QUOTA_ABOVE_MAXIMUM', 'maximum', 5, 4, False),
    ('QUOTA_ABOVE_MAXIMUM', 'maximum', 4, 5, True),
])
def test_quota_breaches_compare_actuals_and_ignore_the_derived_difference(code, comparison, before, after, adds):
    def quota(actual):
        return finding(code=code, shift_id=None, detail={'amount': 3, 'actual': actual,
            'difference': actual - 3, 'comparison': comparison, 'metric': 'duties'})
    result = compare_findings(phase(findings=[quota(before)]), phase(findings=[quota(after)]))
    assert len(result['new_or_worse_findings']) == int(adds)
    assert result['unchanged_existing_count'] == int(not adds)


def test_worsened_nested_capability_deficit_is_not_hidden_by_unchanged_headcount_gap():
    baseline = finding(code='COVERAGE_GAP', detail={'required': 20, 'available': 1, 'derived_findings': [
        {'code': 'CAPABILITY_GAP', 'severity': 'error',
         'detail': {'required': 3, 'available': 1, 'capability_id': 'skill'}}]})
    proposed = copy.deepcopy(baseline)
    proposed['detail']['derived_findings'][0]['detail']['available'] = 0
    result = compare_findings(phase(findings=[baseline]), phase(findings=[proposed]))
    assert not result['no_new_problems']
    assert result['unchanged_existing_count'] == 1
    assert [f['code'] for f in result['new_or_worse_findings']] == ['CAPABILITY_GAP']


def test_worsened_deeply_grouped_finding_uses_every_constraint_observation():
    baseline = finding(code='SCOPED_COVERAGE_GAP', detail={'required': 20, 'available': 1,
        'line': 'primary', 'derived_findings': [
            {'detail': {'required': 3, 'available': 1, 'line': 'secondary', 'derived_findings': [
                {'detail': {'required': 2, 'available': 1, 'line': 'nested'}}]}}]})
    proposed = copy.deepcopy(baseline)
    proposed['detail']['derived_findings'][0]['detail']['derived_findings'][0]['detail']['available'] = 0

    result = compare_findings(phase(findings=[baseline]), phase(findings=[proposed]))

    assert result['comparison_complete'] and not result['no_new_problems']
    assert result['unchanged_existing_count'] == 2
    assert len(result['new_or_worse_findings']) == 1
    assert result['new_or_worse_findings'][0]['detail']['line'] == 'nested'


@pytest.mark.parametrize('side', ['baseline', 'proposed'])
@pytest.mark.parametrize('field', ['findings', 'diagnostics', 'candidate_evidence'])
def test_missing_phase_evidence_cannot_certify_no_new_problems(side, field):
    checks = {'baseline': phase(), 'proposed': phase()}
    del checks[side][field]

    result = compare_findings(checks['baseline'], checks['proposed'])

    assert not result['comparison_complete']
    assert not result['no_new_problems']


@pytest.mark.parametrize('side', ['baseline', 'proposed'])
@pytest.mark.parametrize('status', ['unknown_time_limit', 'model_invalid', 'translation_error',
                                   'invalid_inputs', 'cancelled'])
def test_unsuccessful_phase_cannot_certify_no_new_problems(side, status):
    checks = {'baseline': phase(), 'proposed': phase()}
    checks[side]['status'] = status

    result = compare_findings(checks['baseline'], checks['proposed'])

    assert not result['comparison_complete']
    assert not result['no_new_problems']


@pytest.mark.parametrize('change', ['missing', 'different_person', 'different_shift'])
def test_candidate_identity_mismatch_is_incomplete(change):
    baseline, proposed = phase(), phase()
    if change == 'missing':
        proposed['candidate_evidence'].pop()
    elif change == 'different_person':
        proposed['candidate_evidence'][0]['person_id'] = 'unrelated-person'
    else:
        proposed['candidate_evidence'][0]['shift_id'] = 'unrelated-duty'

    result = compare_findings(baseline, proposed)

    assert not result['comparison_complete']
    assert not result['no_new_problems']


@pytest.mark.parametrize('definition_changed', [False, True])
def test_conflict_core_retains_requirement_definition_but_ignores_display_metadata(definition_changed):
    core = {'code': 'CONFLICTING_REQUIREMENTS', 'proven': True,
            'proof_kind': 'sufficient_set_with_background', 'solver_status': 'INFEASIBLE',
            'requirements': [{'kind': 'policy', 'rule_id': 'quota', 'revision': 1,
                'name': 'Existing weekly quota', 'starts_on': '2026-01-01',
                'definition': {'rule': {'kind': 'block_quota', 'minimum': 4}}}],
            'checked_at': START, 'input_fingerprint': 'baseline', 'elapsed_seconds': 0.1,
            'message': 'Existing requirements conflict'}
    baseline = phase(diagnostics=[core])
    proposed = copy.deepcopy(baseline)
    diagnostic = proposed['diagnostics'][0]
    diagnostic.update(checked_at=END, input_fingerprint='proposed', elapsed_seconds=0.2,
                      message='The same named conflict')
    diagnostic['requirements'][0]['name'] = 'Renamed weekly quota'
    if definition_changed:
        diagnostic['requirements'][0]['definition']['rule']['minimum'] = 5

    result = compare_findings(baseline, proposed)

    assert result['comparison_complete']
    assert result['no_new_problems'] is not definition_changed
    assert len(result['new_or_worse_findings']) == int(definition_changed)
    assert result['unchanged_existing_count'] == int(not definition_changed)


def test_conflict_requirement_order_is_metadata_but_nested_constraint_order_is_semantic():
    core = {'code': 'CONFLICTING_REQUIREMENTS', 'proven': True, 'requirements': [
        {'kind': 'policy', 'rule_id': 'quota', 'definition': {
            'scope': {'conditions': [{'field': 'person_ids'}, {'field': 'service_ids'}]},
            'count_bucket': [['who.0', 'person'], ['where.0', 'clinic']]}},
        {'kind': 'duty', 'shift_id': 'duty'}]}
    baseline = phase(diagnostics=[core])
    reordered = copy.deepcopy(core)
    reordered['requirements'].reverse()
    result = compare_findings(baseline, phase(diagnostics=[reordered]))
    assert result['no_new_problems'] and result['unchanged_existing_count'] == 1
    for field in ('conditions', 'count_bucket'):
        changed = copy.deepcopy(core)
        definition = changed['requirements'][0]['definition']
        if field == 'conditions':
            definition['scope'][field].reverse()
        else:
            definition[field][0].reverse()
        result = compare_findings(baseline, phase(diagnostics=[changed]))
        assert not result['no_new_problems']
        assert len(result['new_or_worse_findings']) == 1
