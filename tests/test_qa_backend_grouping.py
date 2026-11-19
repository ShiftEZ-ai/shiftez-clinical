"""Grouping a person's blocker preserves safety identity and both schedulers' coverage needs."""
import copy

import pytest

from packages.domain.cp_sat import generate as cp_sat_generate
from packages.domain.generation import coverage_findings, deficit_score, citation_finding_key, generate as greedy_generate
from packages.domain.time import aware
from packages.domain.validation import evaluate


def unavailable_assignment(h, locked=False, editable=False):
    typ = h.configure('assignment_types', {'name': 'Grouped coverage regression', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}]}})['record']
    person, spare = h.seed['people'][:2]
    workspace = h.shift(h.workspace(), typ=typ['id'], people=[person])['workspace']
    if editable:
        workspace = h.shift(workspace, typ=typ['id'], people=[],
            start='2026-10-05T08:00:00+03:00', end='2026-10-05T12:00:00+03:00')['workspace']
    data = h.snapshot(workspace)
    shift = next(s for s in data['shifts'] if s['participants'])
    shift['locked'] = locked
    shift['participants'][0]['fixed'] = True
    data['people'] = {pid: data['people'][pid] for pid in (person, spare)}
    data['generation_options'] = {'mode': 'rebalance'}
    data['availability'].append({'id': 'synthetic-leave', 'person_id': person, 'kind': 'approved_leave',
        'start': shift['start'], 'end': shift['end']})
    return data, shift, spare


@pytest.mark.parametrize('generate', [greedy_generate, cp_sat_generate], ids=['greedy', 'cp-sat'])
def test_fixed_unavailable_person_remains_blocking_while_spare_fills_grouped_gap(h, generate):
    data, shift, spare = unavailable_assignment(h)
    original = copy.deepcopy(data)
    before = evaluate(data)
    assert [f['code'] for f in before['findings']] == ['UNAVAILABLE']
    assert before['error_count'] == 1 and deficit_score(before) == (1, 0, 0)
    result = generate(data, 5)
    assert result['status'] == 'candidate_with_gaps', result
    assert result['removals'] == []
    addition, = result['additions']
    assert addition['shift_id'] == shift['id'] and addition['participant']['person_id'] == spare
    after = result['validation']
    assert [f['code'] for f in after['findings']] == ['UNAVAILABLE']
    assert citation_finding_key(before['findings'][0]) == citation_finding_key(after['findings'][0])
    assert after['error_count'] == 1 and not after['valid']
    assert deficit_score(after) == (0, 0, 0) and list(coverage_findings(after)) == []
    assert after['coverage'][0]['available_eligible'] == 1
    assert data == original


def test_supervised_trainee_counts_once_for_surplus_cover(h):
    trainee, supervisor = h.seed['people'][:2]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
              'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Single supervised place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}], 'supervision': {
            'trainee_category_ids': [h.seed['trainee_category']], 'capability_id': h.seed['supervisor_capability'],
            'trainees_per_supervisor': 1, 'require_pairing': True}}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=[supervisor,
        {'person_id': trainee, 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
         'participation': 'trainee', 'supervisor_person_id': supervisor}])['workspace']
    checked = evaluate(h.snapshot(workspace))
    assert not [finding for finding in checked['findings'] if finding['code'] == 'SURPLUS_COVER']


def test_unavailable_nontrainee_does_not_count_toward_surplus_cover(h):
    absent, available = h.seed['people'][:2]
    typ = h.configure('assignment_types', {'name': 'Unavailable ordinary place', 'starts_on': '2020-01-01',
                    'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=[absent, available])['workspace']
    h.command('coordinator', 'availability.record', {'person_id': absent, 'kind': 'recorded_leave',
              'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00', 'private_reason': 'Synthetic'})
    checked = evaluate(h.snapshot(workspace))
    assert not [finding for finding in checked['findings'] if finding['code'] == 'SURPLUS_COVER']


def test_unavailable_trainee_does_not_count_toward_surplus_cover(h):
    trainee, supervisor = h.seed['people'][:2]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
              'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Unavailable supervised place', 'starts_on': '2020-01-01',
                    'definition': {'requirements': [{'minimum': 1}], 'supervision': {
                        'trainee_category_ids': [h.seed['trainee_category']],
                        'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
                        'require_pairing': True}}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=[supervisor,
        {'person_id': trainee, 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
         'participation': 'trainee', 'supervisor_person_id': supervisor}])['workspace']
    h.command('coordinator', 'availability.record', {'person_id': trainee, 'kind': 'recorded_leave',
              'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00', 'private_reason': 'Synthetic'})
    checked = evaluate(h.snapshot(workspace))
    assert not [finding for finding in checked['findings'] if finding['code'] == 'SURPLUS_COVER']


def test_unavailable_trainee_does_not_hide_available_surplus(h):
    trainee, supervisor, available = h.seed['people'][:3]
    h.command('coordinator', 'membership.create', {'person_id': trainee,
              'category_id': h.seed['trainee_category'], 'starts_on': '2020-01-01'})
    typ = h.configure('assignment_types', {'name': 'Unavailable trainee surplus', 'starts_on': '2020-01-01',
                    'definition': {'requirements': [{'minimum': 1}], 'supervision': {
                        'trainee_category_ids': [h.seed['trainee_category']],
                        'capability_id': h.seed['supervisor_capability'], 'trainees_per_supervisor': 1,
                        'require_pairing': True}}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=[supervisor, available,
        {'person_id': trainee, 'start': '2026-10-04T08:00:00+03:00', 'end': '2026-10-04T12:00:00+03:00',
         'participation': 'trainee', 'supervisor_person_id': supervisor}])['workspace']
    h.command('coordinator', 'availability.record', {'person_id': trainee, 'kind': 'recorded_leave',
              'start': '2026-10-04T00:00:00+03:00', 'end': '2026-10-05T00:00:00+03:00', 'private_reason': 'Synthetic'})
    checked = evaluate(h.snapshot(workspace))
    assert any(finding['code'] == 'SURPLUS_COVER' for finding in checked['findings'])


def test_real_surplus_cover_still_warns(h):
    typ = h.configure('assignment_types', {'name': 'Surplus cover place', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 1}]}})['record']['id']
    workspace = h.shift(h.workspace(), typ=typ, people=h.seed['people'][:2])['workspace']
    checked = evaluate(h.snapshot(workspace))
    assert any(finding['code'] == 'SURPLUS_COVER' for finding in checked['findings'])


@pytest.mark.parametrize('generate', [greedy_generate, cp_sat_generate], ids=['greedy', 'cp-sat'])
def test_locked_unavailable_gap_is_preserved_while_editable_session_is_filled(h, generate):
    data, locked, _ = unavailable_assignment(h, locked=True, editable=True)
    original = copy.deepcopy(data)
    editable = next(s for s in data['shifts'] if s['id'] != locked['id'])
    result = generate(data, 5)
    assert result['status'] == 'candidate_with_gaps', result
    assert result['removals'] == []
    addition, = result['additions']
    assert addition['shift_id'] == editable['id']
    assert not result['validation']['valid']
    assert result['validation']['error_count'] == 1
    assert [f['code'] for f in result['validation']['findings']] == ['UNAVAILABLE']
    gap, = coverage_findings(result['validation'])
    assert gap['code'] == 'COVERAGE_GAP' and gap['shift_id'] == locked['id']
    assert aware(gap['start']) == aware(locked['start']) and aware(gap['end']) == aware(locked['end'])
    assert gap['detail'] == {'required': 1, 'available': 0}
    if generate is greedy_generate:
        assert [f['shift_id'] for f in result['unmet_requirements']] == [locked['id']]
    else:
        assert not result['optimality_proven']
    assert data == original


def test_finding_identity_ignores_derived_display_needs_but_retains_safety_detail():
    finding = {'code': 'UNAVAILABLE', 'person_id': 'person', 'shift_id': 'shift',
               'start': '2026-10-04T05:00:00+00:00', 'end': '2026-10-04T09:00:00+00:00',
               'rule_id': None, 'detail': None}
    derived = {'code': 'COVERAGE_GAP', 'severity': 'error', 'detail': {'required': 1, 'available': 0}}
    assert citation_finding_key(finding) == citation_finding_key({**finding, 'detail': {'derived_findings': [derived]}})
    restriction = {**finding, 'code': 'RESTRICTION', 'detail': {'line': 1, 'rule_severity': 'mandatory'}}
    assert citation_finding_key(restriction) == citation_finding_key({**restriction,
        'detail': {**restriction['detail'], 'derived_findings': [derived]}})
    assert citation_finding_key(restriction) != citation_finding_key({**restriction,
        'detail': {**restriction['detail'], 'line': 2}})
