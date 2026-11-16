"""Scoped cover keeps existing gaps elsewhere without relaxing selected staffing."""
import copy

import pytest

from packages.domain.cp_sat import generate
from packages.domain.validation import evaluate, finding_observations


def scoped_cover(h, *, minimum=1, ceiling='duty'):
    typ = h.configure('assignment_types', {
        'name': 'Synthetic optional scoped cover', 'starts_on': '2020-01-01',
        'definition': {'requirements': [{'minimum': 0, 'maximum': 1 if ceiling == 'duty' else 2}]}})['record']['id']
    h.policy({'kind': 'minimum_cover', 'minimum': minimum})
    if ceiling == 'policy':
        h.policy({'kind': 'maximum_participants', 'maximum': 1})
    workspace = h.shift(h.workspace(), typ=typ, people=[])['workspace']
    workspace = h.shift(workspace, typ=typ, people=[], start='2026-10-10T08:00:00+03:00',
                        end='2026-10-10T12:00:00+03:00')['workspace']
    data = h.snapshot(workspace)
    data['people'] = {pid: data['people'][pid] for pid in h.seed['people'][:2]}
    kept, selected = sorted(data['shifts'], key=lambda shift: shift['start'])
    data['generation_session_ids'] = [selected['id']]
    return data, kept['id'], selected['id']


@pytest.mark.parametrize('continuation', [False, True])
def test_scoped_mandatory_cover_retains_only_the_unchanged_unselected_gap(h, continuation):
    data, kept, selected = scoped_cover(h)
    original = copy.deepcopy(data)
    before = [finding for finding in finding_observations(evaluate(data))
              if finding['severity'] == 'error' and finding['shift_id'] == kept]

    result = generate(data, 3, deterministic=True, _diagnostic_continuation=continuation)

    assert data == original
    assert result['status'] == 'candidate_with_gaps', result
    assert not result['diagnostic_proposal'] and not result.get('diagnostic_continued')
    assert [addition['shift_id'] for addition in result['additions']] == [selected]
    assert not result['removals']
    errors = [finding for finding in finding_observations(result['validation']) if finding['severity'] == 'error']
    assert errors == before
    assert {finding['code'] for finding in errors} == {'SCOPED_COVERAGE_GAP'}


@pytest.mark.parametrize('ceiling', ['duty', 'policy'])
def test_scoped_diagnostic_continuation_keeps_selected_cover_strict_until_infeasibility_and_preserves_ceiling(h, ceiling):
    data, kept, selected = scoped_cover(h, minimum=2, ceiling=ceiling)

    strict = generate(data, 3, deterministic=True)
    assert strict['status'] == 'requirements_conflict', strict
    assert not strict['diagnostic_proposal']
    assert not strict['additions'] and not strict['removals']

    result = generate(data, 3, deterministic=True, _diagnostic_continuation=True)

    assert result['status'] == 'candidate_with_gaps', result
    assert result['diagnostic_proposal'] and result['diagnostic_continued'] and result['preparation_reused']
    assert not result['optimality_proven'] and not result['validation']['valid']
    assert [addition['shift_id'] for addition in result['additions']] == [selected]
    assert not result['removals']
    errors = [finding for finding in finding_observations(result['validation']) if finding['severity'] == 'error']
    assert {finding['code'] for finding in errors} == {'SCOPED_COVERAGE_GAP'}
    assert {finding['shift_id']: finding['detail']['available'] for finding in errors} == {kept: 0, selected: 1}
