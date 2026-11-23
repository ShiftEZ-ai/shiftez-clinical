"""Small exact-impact oracles and deterministic work guards; no host timing or scale fixture."""
from copy import deepcopy
import json

import pytest
from sqlalchemy import event, text

from apps.api import snapshots
from apps.api.services import catalogue_preview as preview_service
from apps.api.serialization import json_value
from packages.domain import finding_causes
from packages.domain.validation import evaluate
from packages.domain.finding_identity import finding_index
from .test_catalogue_preview import preview, state
from .test_contact_preview_perf import change, person
from .test_finding_causes import cause_data, cause_snapshot, quota, type_definition, uid


def explained_oracle(prepared):
    """Original three fully explained checks, independently constructing the public comparison."""
    impacts = []
    for w in prepared['workspaces']:
        before, after = evaluate(w['before']), evaluate(w['after'])
        old, new = finding_index(before), finding_index(after)
        impacts.append({'workspace_id': w['workspace_id'], 'name': w['name'], 'status': w['status'],
            'before_error_count': before['error_count'], 'after_error_count': after['error_count'],
            'before_warning_count': before['warning_count'], 'after_warning_count': after['warning_count'],
            'introduced': [f for k, f in new.items() if k not in old],
            'resolved': [f for k, f in old.items() if k not in new],
            'recited': [],
            'saved_draft_validation': evaluate(w['saved']) if w['saved'] is not None else None})
    return json_value({**prepared['response'], 'impacts': impacts})


@pytest.mark.parametrize('direction', ['introduced', 'resolved'])
def test_changed_side_retains_original_finding_key_deduplication(monkeypatch, direction):
    first = {'code': 'COVERAGE_GAP', 'severity': 'error', 'message': 'First display wording',
             'detail': {'required': 1, 'available': 0}}
    last = {**first, 'message': 'Last display wording', 'cause': {'code': 'unfilled'}}
    unchanged = {'findings': [], 'error_count': 0, 'warning_count': 0}
    changed = {'findings': [first, last], 'error_count': 2, 'warning_count': 0}
    checks = {'before': unchanged, 'after': changed} if direction == 'introduced' else {'before': changed, 'after': unchanged}
    monkeypatch.setattr(preview_service, 'evaluate', lambda data, **kwargs: checks[data])
    result = preview_service.evaluate_preview({'response': {}, 'workspaces': [{
        'workspace_id': 'synthetic', 'name': 'Key identity', 'status': 'published',
        'before': 'before', 'after': 'after', 'saved': None}]})['impacts'][0]
    assert result[direction] == [last]
    assert result['after_error_count' if direction == 'introduced' else 'before_error_count'] == 2


@pytest.mark.parametrize('scenario,full_checks', [
    ('unchanged', 1), ('introduced', 2), ('resolved', 2), ('both', 3), ('new_person', 1)])
def test_comparisons_preserve_explained_contract_and_bound_explanation_work(cause_data, monkeypatch, scenario, full_checks):
    data, pid = cause_data
    capability = next(iter(data['capabilities']))
    type_definition(data, extra_cover=True, requirements=[{'minimum': 2,
        'capabilities': [{'capability_id': capability, 'minimum': 1}]}])
    quota(data, pid)
    before, after = deepcopy(data), deepcopy(data)
    if scenario == 'introduced':
        before['policies'] = []
    elif scenario == 'resolved':
        after['policies'] = []
    elif scenario == 'both':
        after['policies'][0]['definition']['rule']['amount'] = 2
    elif scenario == 'new_person':
        new_id = uid('new-person-without-membership')
        after['people'][new_id] = {**deepcopy(after['people'][pid]), 'id': new_id, 'display_name': 'Fictional new person'}
        assert not any(m['person_id'] == new_id for m in after['memberships'])
    prepared = {'response': {'source_fingerprint': 'exact-proposal'}, 'workspaces': [{
        'workspace_id': data['workspace']['id'], 'name': 'Exact comparison', 'status': 'draft',
        'before': before, 'after': after, 'saved': deepcopy(after)}]}
    frozen = deepcopy(prepared)
    original, calls, explanations = preview_service.evaluate, [], []
    init = finding_causes.FindingCauses.__init__

    def explain(self, data):
        explanations.append(data['fingerprint'])
        init(self, data)

    monkeypatch.setattr(finding_causes.FindingCauses, '__init__', explain)
    expected = explained_oracle(prepared)
    assert len(explanations) == 3
    explanations.clear()

    def checked(data, **kwargs):
        calls.append(kwargs.get('explain_findings', True))
        return original(data, **kwargs)

    monkeypatch.setattr(preview_service, 'evaluate', checked)
    actual = preview_service.evaluate_preview(prepared)
    assert actual == expected and prepared == frozen
    assert calls[:2] == [False, False] and sum(calls) == len(explanations) == full_checks
    assert len(calls) == 2 + full_checks
    saved = actual['impacts'][0]['saved_draft_validation']
    assert any((f.get('detail') or {}).get('derived_findings') for f in saved['findings'])
    assert any(f.get('cause') and f.get('fix_hints') for f in saved['findings'])
    print(json.dumps({'scenario': scenario, 'original_explained_checks': 3,
                      'comparison_checks': 2, 'explained_checks': full_checks}))


@pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('published', [False, True])
@pytest.mark.parametrize('operation', ['create', 'archive'])
def test_phase_batched_inputs_match_fresh_snapshots_and_reduce_catalogue_queries(h, monkeypatch, published, operation):
    first = h.shift(h.workspace('First small rota'), people=None if published else [])['workspace']
    h.shift(h.workspace('Second small rota'), people=[])
    if published:
        # Publish a normally staffed rota; the independent empty draft retains coverage explanations.
        h.publish(first)
    h.client('admin')
    row, unchanged = person(h), state(h)
    command = (change(row, active=False) if operation == 'archive' else
               {'kind': 'catalogue.create', 'payload': {'catalogue': 'people', 'value': {
                   'display_name': 'Fictional new person', 'tags': ['New clinical tag']}, 'reason': 'Preview only'}})
    real_inputs, real_snapshot = preview_service.snapshot_inputs, preview_service.domain_snapshot
    phase_inputs, reads, reading, captures = [], {'shared': [], 'fresh': []}, [], []
    capture_phases = []
    catalogue_reads = {'shared': 0, 'fresh': 0}
    project_catalogue = snapshots._snapshot_inputs
    projecting_catalogue = False

    def sql_read(conn, cursor, statement, parameters, context, executemany):
        if reading and statement.lstrip().upper().startswith('SELECT'):
            reads[reading[-1]].append(statement)
            if projecting_catalogue:
                catalogue_reads[reading[-1]] += 1

    def project(db, org):
        nonlocal projecting_catalogue
        projecting_catalogue = True
        try:
            return project_catalogue(db, org)
        finally:
            projecting_catalogue = False

    def inputs(db, org):
        reading.append('shared')
        try:
            result = real_inputs(db, org)
        finally:
            reading.pop()
        phase_inputs.append(result)
        return result

    def snapshot(db, workspace, **kwargs):
        under_writer = bool(db.connection().info.get('rota_write'))
        capture_phases.append((under_writer, db.in_nested_transaction()))
        if not under_writer:
            if db.get_bind().dialect.name == 'postgresql':
                assert db.scalar(text('SHOW transaction_read_only')) == 'on'
                assert db.scalar(text('SHOW transaction_isolation')) == 'repeatable read'
            else:
                assert db.connection().connection.driver_connection.in_transaction
        shared_before = deepcopy(kwargs['shared_inputs'])
        reading.append('shared')
        try:
            result = real_snapshot(db, workspace, **kwargs)
        finally:
            reading.pop()
        reading.append('fresh')
        try:
            fresh = real_snapshot(db, workspace, **{k: v for k, v in kwargs.items()
                                                     if k not in {'shared_inputs', '_event_currency'}})
        finally:
            reading.pop()
        assert result == fresh and kwargs['shared_inputs'] == shared_before
        captures.append(result)
        return result

    real_evaluate = preview_service.evaluate_preview

    def compare(prepared):
        result = real_evaluate(prepared)
        assert result == explained_oracle(prepared)
        return result

    monkeypatch.setattr(preview_service, 'snapshot_inputs', inputs)
    monkeypatch.setattr(preview_service, 'domain_snapshot', snapshot)
    monkeypatch.setattr(preview_service, 'evaluate_preview', compare)
    monkeypatch.setattr(snapshots, '_snapshot_inputs', project)
    event.listen(h.app.state.engine, 'before_cursor_execute', sql_read)
    try:
        result = preview(h, command)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql_read)
    assert len(phase_inputs) == 2 and phase_inputs[0] is not phase_inputs[1]
    if operation == 'archive':
        assert phase_inputs[0]['people'][row['id']]['active'] is True
        assert phase_inputs[1]['people'][row['id']]['active'] is False
    else:
        created = result['proposed']['id']
        assert created not in phase_inputs[0]['people'] and created in phase_inputs[1]['people']
        assert not any(m['person_id'] == created for m in phase_inputs[1]['memberships'])
        assert any(created in t['members'].get('people', []) for t in phase_inputs[1]['tags'].values())
        assert phase_inputs[0]['tags'] != phase_inputs[1]['tags']
    assert phase_inputs[1]['organisation']['input_revision'] == phase_inputs[0]['organisation']['input_revision'] + 1
    assert len(captures) == (5 if published else 6)
    assert capture_phases == [(False, False)] * 2 + [(True, True)] * (len(captures) - 2)
    assert result['workspace_count'] == len(result['impacts']) == 2
    assert state(h) == unchanged and person(h) == row
    # Each catalogue table is scanned once per phase, independently of the workspace count.
    scans = {mode: sum('FROM people' in sql for sql in statements) for mode, statements in reads.items()}
    assert scans == {'shared': 2, 'fresh': len(captures)}
    # The fresh oracle uses the same sessions after the real snapshots, so its
    # baseline and other session-local reads are already warm. Count catalogue projection work.
    assert catalogue_reads['shared'] < catalogue_reads['fresh']
    print(json.dumps({'operation': operation, 'published': published, 'people_scans': scans,
                      'catalogue_selects': catalogue_reads,
                      'selects': {mode: len(statements) for mode, statements in reads.items()}}))
