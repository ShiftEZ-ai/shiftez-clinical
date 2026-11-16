"""Candidate explanations do not preempt changed-plan checks; breach evidence still does."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from apps.api.errors import Problem
from apps.api.services import issues


@pytest.mark.parametrize('nested', [False, True], ids=['primary', 'derived'])
@pytest.mark.parametrize('field', ['cause', 'fix_hints', 'severity', 'detail'])
def test_issue_freshness_distinguishes_candidate_explanations_from_changed_breaches(monkeypatch, field, nested):
    breach = {'code': 'COVERAGE_GAP', 'rule_id': None, 'shift_id': 'shift', 'person_id': None,
              'start': '2026-10-04T08:00:00+03:00', 'severity': 'error', 'detail': {'required': 2, 'scheduled': 1},
              'cause': {'code': 'unfilled', 'eligible_people': 2},
              'fix_hints': [{'action': 'run_auto_schedule', 'prefill': {'shift_ids': ['shift']}}]}
    finding = {**breach, 'code': 'UNAVAILABLE', 'person_id': 'person',
               'detail': {'derived_findings': [breach]}} if nested else breach
    changed = deepcopy(finding)
    target = changed['detail']['derived_findings'][0] if nested else changed
    target[field] = {'cause': {'code': 'no_eligible_people', 'eligible_people': 0},
                     'fix_hints': [{'action': 'open_eligibility', 'prefill': {'shift_ids': ['shift']}}],
                     'severity': 'warning', 'detail': {'required': 3, 'scheduled': 1}}[field]
    validation = {'fingerprint': 'old', 'findings': [finding]}
    before = deepcopy(validation)
    workspace = SimpleNamespace(validation=validation)
    monkeypatch.setattr(issues, 'fingerprint', lambda db, w: 'current')
    monkeypatch.setattr(issues, 'domain_snapshot', lambda db, w: {})
    monkeypatch.setattr(issues, 'evaluate', lambda source: {'findings': [changed]})
    keys = [issues.issue_key(finding)]
    if field in {'cause', 'fix_hints'}:
        assert issues.chosen(None, workspace, keys) == [finding]
    else:
        with pytest.raises(Problem) as error:
            issues.chosen(None, workspace, keys)
        assert error.value.code == 'STALE_CHECK'
    assert workspace.validation == before
