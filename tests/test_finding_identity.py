"""Finding effects follow obligation outcomes, carrying citation changes separately."""
from copy import deepcopy

import pytest

from apps.api.services import catalogue_preview
from packages.contracts.schemas import CatalogueWorkspaceImpact, FindingRecitation, ObservedRuleEffect
from packages.domain.feasibility import compare_findings
from packages.domain.finding_identity import citation, finding_index, finding_key, finding_substance
from packages.domain.rule_effects import observed_effect
from packages.domain.validation import evaluate
from .test_finding_causes import quota


def finding(**changes):
    return {'code': 'QUOTA_ABOVE_MAXIMUM', 'severity': 'error', 'person_id': 'person',
        'shift_id': None, 'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-11T00:00:00+03:00',
        'rule_id': 'before-rule', 'message': 'Duties per week',
        'detail': {'actual': 2, 'amount': 1, 'difference': 1, 'metric': 'duties', 'comparison': 'maximum',
                   'line': 1, 'slot': 'max', 'rule_severity': 'mandatory', 'rule_strength': 'medium'}, **changes}


def preview(monkeypatch, before, after):
    def evaluate(data, **kwargs):
        return {'findings': data, 'error_count': len(data), 'warning_count': 0}
    monkeypatch.setattr(catalogue_preview, 'evaluate', evaluate)
    return catalogue_preview.evaluate_preview({'response': {}, 'workspaces': [{
        'workspace_id': 'draft', 'name': 'Synthetic weekly quota', 'status': 'draft',
        'before': before, 'after': after, 'saved': None}]})['impacts'][0]


def test_pure_recitation_has_no_added_or_cleared_checks(monkeypatch):
    before = finding()
    after = finding(rule_id='after-rule', detail={**before['detail'], 'line': 0, 'rule_tier': 1})
    impact = preview(monkeypatch, [before], [after])
    assert impact['introduced'] == impact['resolved'] == []
    assert impact['recited'][0]['line_number_before'] == 2
    assert impact['recited'][0]['line_number_after'] == 1
    effect = observed_effect({'findings': [before]}, {'findings': [after]})
    assert effect['adds'] == effect['clears'] == 0 and effect['summary'] == 'No change'
    assert effect['recited'] == impact['recited']
    assert ObservedRuleEffect.model_validate(effect).model_dump() == effect
    assert CatalogueWorkspaceImpact.model_validate(impact).model_dump() == impact
    assert FindingRecitation.model_validate(impact['recited'][0]).model_dump() == {
        'finding_key': finding_key(before), 'code': before['code'],
        'person_id': 'person', 'shift_id': None, 'start': before['start'], 'end': before['end'],
        'rule_id_before': 'before-rule', 'rule_id_after': 'after-rule',
        'line_id_before': None, 'line_id_after': None,
        'line_number_before': 2, 'line_number_after': 1}


def test_real_number_change_still_adds_and_clears_in_exact_preview(monkeypatch):
    before = finding()
    after = finding(detail={**before['detail'], 'amount': 0, 'difference': 2})
    impact = preview(monkeypatch, [before], [after])
    assert len(impact['introduced']) == len(impact['resolved']) == 1
    assert impact['recited'] == []
    assert observed_effect({'findings': [before]}, {'findings': [after]})['adds'] == 1


def test_removing_duplicate_grouped_citation_keeps_the_same_obligation(monkeypatch):
    before = finding()
    after = finding(detail={**before['detail'], 'line': 0})
    grouped = deepcopy(before)
    grouped['detail']['derived_findings'] = [after]
    impact = preview(monkeypatch, [grouped], [after])
    assert impact['introduced'] == impact['resolved'] == []
    assert len(impact['recited']) == 1
    effect = observed_effect({'findings': [grouped]}, {'findings': [after]})
    assert effect['adds'] == effect['clears'] == 0
    assert effect['recited'] == impact['recited']


@pytest.mark.parametrize('field,value', [
    ('code', 'OTHER_CHECK'), ('severity', 'warning'), ('person_id', 'other-person'),
    ('shift_id', 'other-session'), ('start', '2026-10-05T00:00:00+03:00'),
    ('end', '2026-10-12T00:00:00+03:00'), ('person_ids', ['a', 'b']),
    ('unknown_constraint', {'limit': 3}),
])
def test_substance_keeps_obligation_context(field, value):
    original = finding()
    assert finding_key(original) != finding_key({**original, field: value})


@pytest.mark.parametrize('field,value', [
    ('amount', 0), ('actual', 3), ('slot', 'min'), ('metric', 'hours'),
    ('count_bucket', [['where.0', 'clinic']]), ('capability_id', 'skill'),
    ('rule_strength', 'high'), ('rule_severity', 'advisory'), ('exception_id', 'exception'),
    ('required', 3), ('maximum', 4), ('affected_shift_ids', ['session-a', 'session-b']),
])
def test_substance_keeps_numbers_and_constraint_dimensions(field, value):
    original = finding()
    assert finding_key(original) != finding_key(finding(detail={**original['detail'], field: value}))


@pytest.mark.parametrize('field', ['key', 'line'])
def test_substance_keeps_nested_facts_even_with_citation_names(field):
    original = finding()
    original['detail']['facts'] = {field: 'first'}
    changed = deepcopy(original)
    nested = changed['detail']['facts']
    nested[field] = 'second'
    assert finding_substance(original)['detail']['facts'] == {field: 'first'}
    assert finding_key(original) != finding_key(changed)


def test_real_validation_recitation_keeps_the_same_substance(h):
    from uuid import uuid4

    workspace = h.shift(h.workspace('Finding identity validation'), people=[])['workspace']
    data = h.snapshot(workspace)
    data['policies'] = []
    data['availability'] = []
    data['quota_context'] = {'complete': True, 'start': '2026-10-01T00:00:00+03:00',
                             'end': '2026-11-01T00:00:00+03:00', 'publications': []}
    pid = h.seed['people'][0]
    policy = quota(data, pid)
    policy['template_line'] = {'id': 'before-line', 'line': 0, 'slot': 'min'}
    recited = deepcopy(data)
    recited['policies'][0]['id'] = str(uuid4())
    recited['policies'][0]['template_line'] = {'id': 'after-line', 'line': 1, 'slot': 'min'}
    before, after = finding_index(evaluate(data)), finding_index(evaluate(recited))
    assert 'QUOTA_BELOW_MINIMUM' in {f['code'] for f in before.values()}
    assert before.keys() == after.keys()
    for key, previous in before.items():
        assert finding_substance(previous) == finding_substance(after[key])
        if previous.get('rule_id'):
            assert citation(previous) != citation(after[key])
    assert any((f.get('detail') or {}).get('line') != (after[key].get('detail') or {}).get('line')
               for key, f in before.items())


def test_comparison_ignores_citation_but_retains_diagnostic_requirement_sources():
    before = finding()
    after = finding(rule_id='after-rule', detail={**before['detail'], 'line': 0})
    phase = lambda f: {'status': 'candidate_feasible', 'findings': [f],
                      'diagnostics': [], 'candidate_evidence': []}
    result = compare_findings(phase(before), phase(after))
    assert result['no_new_problems'] and result['unchanged_existing_count'] == 1
    assert len(result['recited']) == 1
    old, new = phase(before), phase(after)
    old['diagnostics'] = [{'code': 'CONFLICTING_REQUIREMENTS', 'requirements': [{'rule_id': 'old'}]}]
    new['diagnostics'] = [{'code': 'CONFLICTING_REQUIREMENTS', 'requirements': [{'rule_id': 'new'}]}]
    assert not compare_findings(old, new)['no_new_problems']


def test_grouped_obligations_compare_independently_of_the_visible_primary(monkeypatch):
    first = finding()
    second = finding(detail={**first['detail'], 'amount': 0, 'difference': 2, 'line': 0})
    before, after = deepcopy(first), deepcopy(second)
    before['detail']['derived_findings'] = [second]
    after['detail']['derived_findings'] = [first]
    impact = preview(monkeypatch, [before], [after])
    assert impact['introduced'] == impact['resolved'] == impact['recited'] == []


def test_optional_absence_explanations_do_not_change_the_obligation():
    plain = finding(code='UNAVAILABLE', detail=None)
    decorated = {**plain, 'classification': 'approved_leave', 'leave_shift_ids': ['leave-session'],
                 'detail': {'classification': 'approved_leave', 'leave_shift_ids': ['leave-session']}}
    assert finding_key(plain) == finding_key(decorated)


def test_line_id_only_change_is_a_recitation(monkeypatch):
    before = finding(detail={'line_id': 'old-line'})
    after = finding(detail={'line_id': 'new-line'})
    impact = preview(monkeypatch, [before], [after])
    assert impact['introduced'] == impact['resolved'] == []
    assert impact['recited'][0]['line_id_before'] == 'old-line'
    assert impact['recited'][0]['line_id_after'] == 'new-line'


@pytest.mark.parametrize('database', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
@pytest.mark.parametrize('maximum', [1, 0], ids=['recitation', 'number-change'])
def test_removing_person_line_keeps_broader_coverage_in_preview_job_and_review(request, database, maximum, monkeypatch):
    from apps.worker.main import process_one
    from packages.domain import feasibility
    from .test_rule_catalogue_api import approve, line, person, propose

    h = request.getfixturevalue(database)
    broad = line(numbers={'max': {'value': 1, 'level': 'mandatory'}})
    specific = line(who=person(h.seed['people'][0]), numbers={'max': {'value': 1, 'level': 'mandatory'}})
    current = approve(h, propose(h, 'duties_per_week', [broad, specific], acknowledged=[broad['id']])['record'])
    workspace = h.shift(h.workspace('Weekly quota citation regression'))['workspace']
    workspace = h.shift(workspace, start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    before = h.snapshot(workspace)
    replacement = {**broad, 'numbers': {'max': {'value': maximum, 'level': 'mandatory'}}}
    pending = propose(h, 'duties_per_week', [replacement], policy_id=current['id'], revision=current['revision'])['record']
    after = {**before, 'policies': [{**pending, 'status': 'active'} if p['id'] == current['id'] else p
                                  for p in before['policies']]}
    impact = catalogue_preview.evaluate_preview({'response': {}, 'workspaces': [{
        'workspace_id': workspace['id'], 'name': workspace['name'], 'status': 'draft',
        'before': before, 'after': after, 'saved': None}]})['impacts'][0]
    effect = observed_effect(evaluate(before), evaluate(after))
    assert len(impact['introduced']) == len(impact['resolved']) == int(maximum != 1)
    assert len(impact['recited']) == int(maximum == 1)
    if maximum == 1:
        assert effect['summary'] == 'No change' and effect['adds'] == effect['clears'] == 0
        assert impact['recited'][0]['line_number_before'] == 2
        assert impact['recited'][0]['line_number_after'] == 1
    else:
        assert effect['adds'] == 1 and not effect['recited']
    monkeypatch.setattr(feasibility, 'generate', lambda *a, **kw: {'status': 'unknown_time_limit'})
    job = h.command('coordinator', 'job.create', {'workspace_id': workspace['id'], 'kind': 'feasibility',
        'rule_changes': [{'policy_id': pending['id'], 'expected_revision': pending['revision']}]},
        revision=workspace['revision'])['job']
    assert process_one(h.app.state.engine)
    checked = h.get('coordinator', '/api/jobs/' + job['id'])['job']['result']
    assert checked['effect'] == effect and not checked['full_feasibility_established']
    review = h.get('approver', f"/api/policies/{pending['id']}/review")
    assert review['effect'][0]['recited'] == impact['recited']
    assert review['effect'][0]['adds'] == effect['adds']
