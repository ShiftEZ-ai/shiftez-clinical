"""Bulk counting proposals retain nominal windows through approval and draft session imports."""
import pytest

from packages.contracts.schemas import BulkApplyResult
from .test_bulk import REASON, preview
from .test_bulk_draft_session_creation import batch
from .test_bulk_series_counting import apply as apply_counting
from .test_department_quota_calendar import settings
from .test_quota_proration_api import rows_for
from .test_rule_catalogue_api import approve, line, person, propose


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_bulk_counting_approval_prorates_lower_bound_and_keeps_draft_findings(request, backend):
    h = request.getfixturevalue(backend)
    settings(h, operating_calendar_id=None, weekend_days=[4, 5])
    counting = {'starts_on': '2026-10-04', 'ends_on': '2026-10-11',
        'window_alignment': 'fixed', 'window_days': 7, 'window_anchor_on': '2026-10-04'}
    authored = line(who=person(h.seed['people'][0]), numbers={'min': {'value': 6}, 'max': {'value': 6}},
                    settings={'block_quota': counting})
    original = approve(h, propose(h, 'duties_per_period', [authored])['record'])
    body = {'item_kind': 'rule.propose', 'selection': {'collection': 'policy_lines',
        'include_ids': [authored['id']]}, 'changes': [{'field': 'counting',
            'value': {'starts_on': '2026-10-08'}}], 'reason': REASON}
    checked = preview(h, 'coordinator', body)
    assert checked['counts']['waiting'] == 1
    proposed = apply_counting(h, 'coordinator', body, checked)
    pending = proposed['items'][0]['results'][0]['record']
    assert pending['status'] == 'proposed'
    [changed] = pending['definition']['rule']['lines']
    assert changed['id'] == authored['id']
    assert changed['settings']['block_quota'] == {**counting, 'starts_on': '2026-10-08'}
    assert original['definition']['rule']['lines'][0]['settings']['block_quota'] == counting
    active = approve(h, pending)

    workspace = h.workspace('Synthetic approved bulk counting window', '2026-10-08', '2026-10-11')
    applied = h.command('coordinator', 'bulk.apply', batch(h, workspace))
    typed = BulkApplyResult.model_validate(applied)
    assert typed.counts['changed'] == 2
    detail = h.detail(workspace)
    saved = {shift['id'] for shift in detail['shifts']}
    assert len(saved) == 2 and typed.warnings
    assert {finding['shift_id'] for finding in typed.warnings if finding.get('shift_id')} == saved
    assert all(finding['workspace_id'] == workspace['id'] and finding['severity'] == 'error'
               for finding in typed.warnings)
    impact = next(impact for impact in typed.impacts if impact.workspace_id == workspace['id'])
    assert {finding['shift_id'] for finding in impact.introduced if finding.get('shift_id')} == saved
    validation = detail['workspace']['validation']
    assert not validation['valid'] and impact.after_error_count == validation['error_count']
    rows = rows_for(validation, active)
    assert (rows['minimum']['amount'], rows['minimum']['actual']) == (2, 0)
    assert rows['minimum']['proration'] == {'configured_amount': 6, 'amount': 2,
        'working_days': 1, 'full_working_days': 5, 'starts_on': '2026-10-04',
        'ends_on': '2026-10-11', 'calendar_id': None, 'rounding': 'up'}
    assert rows['minimum']['display'] == '0 of 2 (prorated: 1 of 5 days)'
    assert (rows['maximum']['amount'], rows['maximum']['actual']) == (6, 0)
    assert not rows['maximum'].get('proration') and rows['maximum']['display'] == '0 of 6'


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_undoing_a_bulk_counting_proposal_retires_it_into_history(request, backend):
    h = request.getfixturevalue(backend)
    authored = line(who=person(h.seed['people'][0]), numbers={'min': {'value': 2}},
                    settings={'block_quota': {'starts_on': '2026-10-01', 'ends_on': '2027-01-01',
                                              'window_alignment': 'fixed', 'window_days': 7}})
    original = approve(h, propose(h, 'duties_per_period', [authored])['record'])
    body = {'item_kind': 'rule.propose', 'selection': {'collection': 'policy_lines', 'include_ids': [authored['id']]},
            'changes': [{'field': 'counting', 'value': {'starts_on': '2026-11-01'}}], 'reason': REASON}
    proposed = apply_counting(h, 'coordinator', body, preview(h, 'coordinator', body))
    pending = proposed['items'][0]['results'][0]['record']
    assert pending['status'] == 'proposed'
    h.command('coordinator', 'bulk.apply', proposed['undo']['payload'])
    # The operational catalogue lists live versions only; the undone proposal is retained as retired history.
    live = {p['id']: p['status'] for p in h.get('admin', '/api/catalogues')['policies']}
    assert pending['id'] not in live and live[original['id']] == 'active'
    retired = {p['id']: p for p in h.get('admin', '/api/policies?status=retired')['policies']}
    assert retired[pending['id']]['status'] == 'retired'
    assert [l['settings']['block_quota']['starts_on'] for l in retired[pending['id']]['definition']['rule']['lines']] == ['2026-11-01']
    assert not h.get('admin', '/api/policies?status=proposed')['policies']
