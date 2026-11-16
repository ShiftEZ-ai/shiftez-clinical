"""Contact previews skip identical scheduling inputs, never tag reconciliation or clinical changes."""
import hashlib
import json
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import catalogue_preview
from apps.api.serialization import record
from packages.contracts.schemas import PersonInput
from .test_catalogue_preview import apply, preview, state
from .test_rule_conditions import line, tree


@pytest.fixture(params=['sqlite', pytest.param('postgres', marks=pytest.mark.postgres)])
def dbh(request):
    return request.getfixturevalue('h' if request.param == 'sqlite' else 'pg')


def person(h):
    return next(p for p in h.get('coordinator', '/api/catalogues')['people'] if p['id'] == h.seed['people'][0])


def change(row, **changes):
    return {'kind': 'catalogue.update', 'target_id': row['id'], 'expected_revision': row['revision'],
            'payload': {'catalogue': 'people', 'value': {
                **{k: row[k] for k in PersonInput.model_fields if k in row}, **changes},
                'reason': 'Update fictional contact details'}}


def forbid_evaluation(*_):
    pytest.fail('Unchanged scheduling inputs must not revalidate every rota')


def forbid_snapshot(*_, **__):
    pytest.fail('A checked contact-only write must not build full rota snapshots')


@pytest.mark.parametrize('field,value', [
    ('title', 'Dr'), ('work_phone', '5550100'), ('work_extension', '1234'),
    ('pager', '987'), ('directory_section', 'Fictional clinic'), ('directory_order', 42)])
def test_each_contact_field_keeps_preview_rollback_and_normal_validation(h, monkeypatch, field, value):
    workspace = h.shift(h.workspace())['workspace']
    before, detail, row = state(h), h.detail(workspace), person(h)
    monkeypatch.setattr(catalogue_preview, 'evaluate', forbid_evaluation)
    monkeypatch.setattr(catalogue_preview, 'domain_snapshot', forbid_snapshot)
    result = preview(h, change(row, **{field: value}), role='coordinator')
    assert result['workspace_count'] == 1 and result['impacts'] == [] and not result['committed']
    assert 'Scheduling inputs are unchanged' in result['message']
    assert result['proposed'][field] == value
    assert state(h) == before and h.detail(workspace) == detail and person(h) == row


def test_contact_with_synced_tags_needs_no_rota_snapshots(dbh, monkeypatch):
    h = dbh
    row = person(h)
    h.configure('people', change(row, tags=['Clinical tag'])['payload']['value'], row['id'], row['revision'])
    row = person(h)
    workspace = h.shift(h.workspace())['workspace']
    before, snapshot = state(h), h.snapshot(workspace)
    monkeypatch.setattr(catalogue_preview, 'evaluate', forbid_evaluation)
    monkeypatch.setattr(catalogue_preview, 'domain_snapshot', forbid_snapshot)
    result = preview(h, change(row, work_phone='5550100'))
    assert result['impacts'] == [] and 'Scheduling inputs are unchanged' in result['message']
    assert state(h) == before and person(h) == row and h.snapshot(workspace) == snapshot


def scale(h):
    """Eight real draft rotas, 150 people, 500 sessions and 1,000 rules including retired history."""
    with transaction(h.app.state.engine, write=True) as db:
        db.add_all(m.Person(organisation_id=h.seed['organisation_id'], display_name=f'Contact scale {n:03d}')
                   for n in range(150 - len(h.seed['people'])))
        typ = db.get(m.AssignmentType, h.seed['type'])
        workspaces = [m.Workspace(department_id=h.seed['department_id'], name=f'Contact scale rota {n}',
            starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11),
            created_by=h.seed['identities']['coordinator'], last_edited_by=h.seed['identities']['coordinator']) for n in range(8)]
        db.add_all(workspaces)
        db.flush()
        for n in range(500):
            start = datetime(2026, 10, 4, 8, tzinfo=timezone.utc) + timedelta(days=n % 7)
            shift = m.Shift(workspace_id=workspaces[n % 8].id, assignment_type_id=typ.id,
                type_revision=typ.revision, type_snapshot=record(typ),
                location_id=h.seed['locations'][0], start=start, end=start + timedelta(hours=4))
            db.add(shift)
            db.flush()
            db.add(m.ShiftService(shift_id=shift.id, service_id=h.seed['services'][0]))
            db.add(m.Assignment(shift_id=shift.id, person_id=h.seed['people'][0], start=start, end=shift.end))
        db.add_all(m.Policy(department_id=h.seed['department_id'], name=f'Contact rule history {n:04d}',
            kind='min_rest', definition={'rule': {'kind': 'min_rest', 'hours': 12}, 'severity': 'mandatory', 'scope': {}},
            status='active' if n < 20 else 'retired', starts_on=date(2020, 1, 1),
            owner='Synthetic owner', source='Performance fixture', created_by=h.seed['identities']['coordinator'],
            approved_by=h.seed['identities']['approver']) for n in range(1000))


def contact_preview_measurement(h, monkeypatch):
    scale(h)
    h.client('admin')  # Measure the preview request, excluding the helper's first sign-in.
    row, before = person(h), state(h)
    cmd = change(row, work_phone='5550100')
    # Measure each path in both clocks: external database work belongs in the nightly wall comparison.
    with monkeypatch.context() as patch:
        patch.setattr(catalogue_preview, 'contact_update', lambda *_: False)
        started, wall_started = time.process_time(), time.perf_counter()
        original = preview(h, cmd)
        baseline = time.process_time() - started
        wall_baseline = time.perf_counter() - wall_started
    monkeypatch.setattr(catalogue_preview, 'evaluate', forbid_evaluation)
    monkeypatch.setattr(catalogue_preview, 'domain_snapshot', forbid_snapshot)
    started, wall_started = time.process_time(), time.perf_counter()
    result = preview(h, cmd)
    elapsed = time.process_time() - started
    wall_elapsed = time.perf_counter() - wall_started
    evidence = {'clock': 'process_time', 'database': h.app.state.engine.dialect.name, 'people': 150, 'sessions': 500, 'rules': 1000,
                'active_rules': 20, 'workspaces': 8, 'seconds': elapsed, 'original_path_seconds': baseline,
                'wall_seconds': wall_elapsed, 'original_path_wall_seconds': wall_baseline,
                'source_sha256': hashlib.sha256(Path(catalogue_preview.__file__).read_bytes()).hexdigest()}
    print(json.dumps(evidence, indent=2))
    if output := os.getenv('AUDIT_PERFORMANCE_RECEIPTS'):
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f'contact-preview-{evidence["database"]}.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
    return h, row, before, cmd, original, result, elapsed, baseline, evidence


@pytest.fixture(scope='module', params=['h', pytest.param('pg', marks=pytest.mark.postgres)])
def contact_preview_scale_measurement(request, scale_harness):
    with pytest.MonkeyPatch.context() as monkeypatch:
        return contact_preview_measurement(scale_harness(request.param), monkeypatch)


def test_contact_preview_at_department_scale_and_checked_undo(contact_preview_scale_measurement, monkeypatch):
    h, row, before, cmd, original, result, _, _, _ = contact_preview_scale_measurement
    monkeypatch.setattr(catalogue_preview, 'evaluate', forbid_evaluation)
    monkeypatch.setattr(catalogue_preview, 'domain_snapshot', forbid_snapshot)
    assert original['workspace_count'] == len(original['impacts']) == 8
    assert all(not i['introduced'] and not i['resolved'] for i in original['impacts'])
    assert result['workspace_count'] == 8 and result['impacts'] == []
    assert state(h) == before and person(h) == row
    saved = apply(h, cmd, result['source_fingerprint'])
    assert saved['record']['work_phone'] == '5550100' and saved['record']['revision'] == row['revision'] + 1
    undo = change(saved['record'], **saved['previous_value'])
    checked = preview(h, undo)
    assert checked['impacts'] == [] and checked['workspace_count'] == 8
    restored = apply(h, undo, checked['source_fingerprint'])['record']
    assert {k: restored[k] for k in PersonInput.model_fields} == {k: row[k] for k in PersonInput.model_fields}
    assert restored['revision'] == row['revision'] + 2
    assert state(h)[-2] == before[-2] + 2
    assert apply(h, cmd, result['source_fingerprint'], status=409)['error']['code'] == 'STALE_PREVIEW'


@pytest.mark.cpu_budget
def test_contact_preview_at_department_scale_time(contact_preview_scale_measurement):
    _, _, _, _, _, _, elapsed, baseline, evidence = contact_preview_scale_measurement
    # Both paths pay shared-host/coverage overhead; the guards independently forbid the expensive scheduling work.
    assert elapsed < baseline, evidence


@pytest.mark.wall_benchmark
def test_contact_preview_at_department_scale_wall_benchmark(contact_preview_scale_measurement):
    *_, evidence = contact_preview_scale_measurement
    elapsed, baseline = evidence['wall_seconds'], evidence['original_path_wall_seconds']
    assert elapsed < baseline, evidence


@pytest.mark.parametrize('drift', ['remove_membership', 'add_membership', 'extend_labels', 'create_tag'])
def test_contact_edit_with_tag_drift_still_runs_full_impact(dbh, drift):
    h = dbh
    workspace = h.shift(h.workspace())['workspace']
    with transaction(h.app.state.engine, write=True) as db:
        target = db.get(m.Person, h.seed['people'][0])
        target.tags = [] if drift == 'remove_membership' else ['Clinical tag']
        if drift != 'create_tag':
            tag = m.Tag(department_id=h.seed['department_id'], name='Clinical tag', labels=['people'])
            db.add(tag)
            db.flush()
            tag_id = tag.id
            if drift == 'remove_membership':
                db.add(m.TagMember(department_id=h.seed['department_id'], tag_id=tag.id,
                                   kind='people', member_id=target.id))
    h.policy({'kind': 'restriction', 'weekdays': [6], 'start_time': '00:00', 'end_time': '24:00'},
             {'tags': ['Clinical tag']} if drift == 'create_tag' else {'conditions': tree(line('person_ids', tag_id))})
    if drift == 'extend_labels':
        with transaction(h.app.state.engine, write=True) as db:
            db.get(m.Tag, tag_id).labels = ['services']  # Legacy inconsistent metadata predates this contact edit.
    h.client('admin')  # Sign-in evidence belongs to setup, not the preview rollback baseline.
    row, before = person(h), state(h)
    snapshot = h.snapshot(workspace)
    result = preview(h, change(row, work_phone='5550100'))
    assert len(result['impacts']) == 1 and 'Scheduling inputs are unchanged' not in result['message']
    impact = result['impacts'][0]
    assert impact['saved_draft_validation'] is not None
    if drift == 'create_tag':
        # The legacy name already restricts this person. Creating its missing tag must still take the full path,
        # even though the evaluator confirms the same restriction before and after the reconciliation.
        assert not impact['introduced'] and not impact['resolved']
        assert any(f['code'] == 'RESTRICTION' for f in impact['saved_draft_validation']['findings'])
    else:
        findings = impact['resolved'] if drift == 'remove_membership' else impact['introduced']
        assert any(f['code'] == 'RESTRICTION' for f in findings), impact
    assert person(h) == row and state(h) == before and h.snapshot(workspace) == snapshot


@pytest.mark.parametrize('changes', [
    {'active': False}, {'tags': ['Clinical tag']}, {'display_name': 'Changed clinical label'},
    {'staff_number': 'Changed staff number'}, {'short_code': 'TEST1'}, {'colour': 'blue'}])
def test_mixed_contact_and_other_fields_use_full_impact(h, changes):
    h.shift(h.workspace())
    result = preview(h, change(person(h), work_phone='5550100', **changes))
    assert len(result['impacts']) == 1 and result['impacts'][0]['saved_draft_validation'] is not None
    if changes.get('active') is False:
        assert result['impacts'][0]['after_error_count'] > result['impacts'][0]['before_error_count']


def test_partial_contact_payload_cannot_hide_reset_of_clinical_fields(h):
    h.shift(h.workspace())
    row = person(h)
    saved = h.configure('people', change(row, active=False)['payload']['value'], row['id'], row['revision'])['record']
    cmd = change(saved, work_phone='5550100')
    del cmd['payload']['value']['active']  # PersonInput's default would reactivate the person.
    result = preview(h, cmd)
    assert len(result['impacts']) == 1 and result['proposed']['active'] is True
    assert result['impacts'][0]['after_error_count'] < result['impacts'][0]['before_error_count']


def test_contact_preview_keeps_schema_role_revision_and_fingerprint_checks(h):
    h.shift(h.workspace())
    h.client('admin'); h.client('staff')
    row, before = person(h), state(h)
    cmd = change(row, work_phone='5550100')
    preview(h, change(row, work_phone='x' * 81), status=422)
    preview(h, cmd, role='staff', status=403)
    preview(h, {**cmd, 'expected_revision': row['revision'] + 1}, status=409)
    checked = preview(h, cmd)
    changed = change(row, work_phone='5550101')
    assert preview(h, {**changed, 'expected_fingerprint': checked['source_fingerprint']}, status=409)['error']['code'] == 'STALE_PREVIEW'
    assert state(h) == before and person(h) == row
