"""Receipt-bound draft Undo restores old findings without accepting new safety defects."""
from copy import deepcopy

import pytest

from apps.api.services.catalogue_preview import finding_key
from packages.domain.validation import evaluate
from .test_bulk import REASON, preview
from .test_bulk_draft_session_creation import creation, state


DATABASES = ['h', pytest.param('pg', marks=pytest.mark.postgres)]


def filled_gap(h):
    rule = h.policy({'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'},
                    {'service_ids': h.seed['services'][:2]})
    workspace = h.shift(h.workspace('Synthetic draft cover restoration'))['workspace']
    original = h.snapshot(workspace)
    item = creation(h, workspace)
    item['payload']['service_ids'] = [h.seed['services'][1]]
    item['payload']['participants'] = [{'person_id': h.seed['people'][1],
        'start': item['payload']['start'], 'end': item['payload']['end']}]
    result = h.command('coordinator', 'bulk.apply', {'items': [item], 'reason': REASON})
    assert any(f['code'] == 'SCOPED_COVERAGE_GAP' for impact in result['impacts'] for f in impact['resolved'])
    assert h.detail(workspace)['workspace']['validation']['valid']
    return workspace, original, result, rule


@pytest.mark.parametrize('database', DATABASES)
def test_bulk_shift_undo_restores_original_mandatory_gap_and_keeps_it_visible(request, database):
    h = request.getfixturevalue(database)
    workspace, original, result, _ = filled_gap(h)
    saved = state(h, workspace)
    checked = preview(h, 'coordinator', result['undo']['payload'])
    assert checked['counts']['ready'] == 1 and checked['counts']['problem'] == 0
    assert any(f['severity'] == 'error' for f in checked['warnings'])
    assert state(h, workspace) == saved
    undone = h.command('coordinator', **result['undo'])
    assert undone['counts']['changed'] == 1
    restored = h.snapshot(workspace)
    assert restored['shifts'] == original['shifts']
    check = evaluate(restored)
    assert not check['valid'] and any(f['code'] == 'SCOPED_COVERAGE_GAP' for f in check['findings'])
    current = h.detail(workspace)['workspace']
    assert h.lifecycle(current, 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'


@pytest.mark.parametrize('database', DATABASES)
def test_bulk_shift_undo_without_its_receipt_cannot_restore_a_mandatory_gap(request, database):
    h = request.getfixturevalue(database)
    workspace, _, result, _ = filled_gap(h)
    forged = deepcopy(result['undo']['payload'])
    forged.pop('undo_of')
    before = state(h, workspace)
    assert preview(h, 'coordinator', forged)['counts']['problem'] == 1
    assert h.command('coordinator', 'bulk.apply', forged, status=409)['error']['code'] == 'SCOPED_COVERAGE_GAP'
    assert state(h, workspace) == before


@pytest.mark.parametrize('database', DATABASES)
def test_bulk_shift_undo_keeps_receipt_authority_when_an_identical_rule_recites_the_gap(request, database):
    h = request.getfixturevalue(database)
    workspace, _, result, _ = filled_gap(h)
    h.policy({'kind': 'minimum_cover', 'minimum': 2, 'aggregation': 'service_group'},
             {'service_ids': h.seed['services'][:2]})
    checked = preview(h, 'coordinator', result['undo']['payload'])
    assert checked['counts']['ready'] == 1 and checked['counts']['problem'] == 0
    assert h.command('coordinator', **result['undo'])['counts']['changed'] == 1
    assert not evaluate(h.snapshot(workspace))['valid']


@pytest.mark.parametrize('database', DATABASES)
@pytest.mark.parametrize('severity', ['mandatory', 'advisory'])
def test_bulk_shift_undo_rechecks_new_requirements_and_only_mandatory_ones_block(request, database, severity):
    h = request.getfixturevalue(database)
    workspace, _, result, _ = filled_gap(h)
    h.policy({'kind': 'minimum_cover', 'minimum': 3, 'aggregation': 'service_group'},
             {'service_ids': h.seed['services'][:2]}, severity=severity)
    before = state(h, workspace)
    checked = preview(h, 'coordinator', result['undo']['payload'])
    assert checked['counts']['problem'] == int(severity == 'mandatory')
    assert 'SCOPED_COVERAGE_GAP' in {f['code'] for f in checked['warnings']}
    if severity == 'mandatory':
        assert h.command('coordinator', **result['undo'], status=409)['error']['code'] == 'SCOPED_COVERAGE_GAP'
        assert state(h, workspace) == before
    else:
        assert h.command('coordinator', **result['undo'])['counts']['changed'] == 1
        assert not evaluate(h.snapshot(workspace))['valid']


def test_restoration_finding_identity_ignores_explanations_but_keeps_safety_evidence():
    finding = {'code': 'SCOPED_COVERAGE_GAP', 'severity': 'error', 'rule_id': 'synthetic-rule',
               'message': 'Earlier wording', 'detail': {'required': 2, 'available': 1}}
    assert finding_key(finding) == finding_key({**finding, 'message': 'Current wording'})
    assert finding_key(finding) != finding_key({**finding, 'detail': {'required': 3, 'available': 1}})
