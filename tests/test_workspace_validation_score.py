"""Parity gate for the score-only evaluation used by workspace reads."""
import copy
import hashlib
import json
import uuid

import pytest

from packages.domain.rota_summary import fix_list_context, issue_key, rota_summary
from packages.domain.score import schedule_score
from packages.domain.validation import evaluate
from tests.test_schedule_score_scale import department, receipt_bytes


def workspace_score_comparison(state, request):
    data = department()
    # Extend the established filled fixture to 500 sessions, retaining its leave and restriction findings.
    for original in data['shifts'][:50]:
        shift = copy.deepcopy(original)
        shift['id'] = str(uuid.uuid5(uuid.UUID(original['id']), 'extra-session'))
        for participant in shift['participants']:
            participant['id'] = str(uuid.uuid5(uuid.UUID(participant['id']), 'extra-session'))
        data['shifts'].append(shift)
        data['focus_ids'].append(shift['id'])
    assert len(data['people']) == 150 and len(data['shifts']) == 500
    if state == 'unassigned':
        for shift in data['shifts']:
            shift['participants'] = []
    elif state == 'no_sessions':
        data['shifts'] = []
        data['focus_ids'] = []
    else:
        assert all(shift['participants'] for shift in data['shifts'])

    visible = []
    for explain in (True, False):
        snapshot = copy.deepcopy(data)
        check = evaluate(snapshot, explain_findings=explain)
        score = schedule_score(snapshot, check)
        check = {**check, 'score': score, 'fix_list_context': fix_list_context(snapshot)}
        summary = rota_summary(check, snapshot['shifts'])
        visible.append({
            'score_bytes': receipt_bytes(score).decode('utf-8'),
            'valid': check['valid'],
            'error_count': check['error_count'],
            'warning_count': check['warning_count'],
            'findings_in_order': [(issue_key(f), *[f.get(key) for key in
                ('id', 'key', 'code', 'rule_id', 'shift_id', 'person_id', 'start', 'end', 'severity')])
                for f in check['findings']],
            'coverage': check['coverage'],
            'summary_counts': {key: value for key, value in summary.items()
                               if isinstance(value, (int, float)) or key == 'issues'},
            'fix_list_counts': {key: value for key, value in summary['fix_list']['summary'].items()
                                if isinstance(value, (int, float))},
        })
        if state != 'no_sessions':
            assert check['findings']
    differences = {key: {'explained': visible[0][key], 'unexplained': visible[1][key]}
                   for key in visible[0] if visible[0][key] != visible[1][key]}
    evidence = {'state': state, 'people': len(data['people']), 'sessions': len(data['shifts']),
                'explained_sha256': hashlib.sha256(receipt_bytes(visible[0])).hexdigest(),
                'unexplained_sha256': hashlib.sha256(receipt_bytes(visible[1])).hexdigest(),
                'different_fields': list(differences),
                'explained_counts': visible[0]['fix_list_counts'],
                'unexplained_counts': visible[1]['fix_list_counts']}
    request.node.user_properties.append(('workspace_score_parity', json.dumps(evidence, sort_keys=True)))
    print('workspace score parity', evidence)
    return visible, differences


@pytest.mark.parametrize('state', ['unassigned', 'no_sessions'])
def test_workspace_score_and_visible_counts_do_not_depend_on_explanations(state, request):
    _, differences = workspace_score_comparison(state, request)
    assert not differences, differences


def test_must_count_depends_on_explanations_until_classification_split(request):
    """Must counts repair groups keyed on explanation diagnoses (packages/domain/rota_summary.py ~137-150, len(must) ~462); explanations must stay ON on any Must-showing path until maintenance's classification/explanation split (post-maintenance card) — then this guard flips to equality."""
    visible, differences = workspace_score_comparison('filled', request)
    assert set(differences) == {'fix_list_counts'}, differences
    explained, unexplained = (dict(value['fix_list_counts']) for value in visible)
    assert explained.pop('must') != unexplained.pop('must')
    assert explained == unexplained
