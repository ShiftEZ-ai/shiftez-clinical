"""Internal trials omit explanations, never constraint observations or score inputs."""
import copy

import pytest

from packages.domain import finding_causes
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate
from .test_finding_causes import cause_data, cause_snapshot, quota, type_definition


def without_explanations(value):
    if isinstance(value, dict):
        return {key: without_explanations(item) for key, item in value.items() if key not in {'cause', 'fix_hints'}}
    if isinstance(value, list):
        return [without_explanations(item) for item in value]
    return value


@pytest.mark.parametrize('mode', ['whole', 'quota', 'partial'])
def test_internal_checks_preserve_all_observations_and_skip_explanation_work(cause_data, monkeypatch, mode):
    data, pid = cause_data
    capability = next(iter(data['capabilities']))
    type_definition(data, extra_cover=True,
        requirements=[{'minimum': 1, 'capabilities': [{'capability_id': capability, 'minimum': 1}]}])
    quota(data, pid)
    if mode == 'partial':
        data['_partial'] = {'people': {pid}, 'shifts': set(data['focus_ids']), 'include_resources': True}
    before = copy.deepcopy(data)
    kwargs = {'quota_only': mode == 'quota'}
    full = evaluate(data, **kwargs)
    assert without_explanations(full) != full

    def unexpected(*args, **kwargs):
        pytest.fail('Internal decision trials must not construct finding explanations')

    monkeypatch.setattr(finding_causes, 'FindingCauses', unexpected)
    lean = evaluate(data, explain_findings=False, **kwargs)
    assert lean == without_explanations(full)
    assert data == before
    if mode == 'whole':
        assert schedule_score(data, lean) == schedule_score(data, full)
        assert any((finding.get('detail') or {}).get('derived_findings') for finding in lean['findings'])


@pytest.mark.parametrize('modes', [(False, True, False), (True, False, True)])
def test_session_cache_keeps_explanation_modes_separate(cause_data, modes):
    data, _ = cause_data
    cache = {}
    full = evaluate(data)
    assert without_explanations(full) != full
    for explain in modes:
        checked = evaluate(data, session_cache=cache, explain_findings=explain)
        assert checked == (full if explain else without_explanations(full))
