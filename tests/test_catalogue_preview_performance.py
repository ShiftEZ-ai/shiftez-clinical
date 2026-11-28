"""Exact-impact oracles and deterministic work guards for catalogue previews."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json

import pytest
from sqlalchemy import event, func, select, text

from apps.api import models as m, snapshots
from apps.api.auth import Actor
from apps.api.db import transaction
from apps.api.services import catalogue_preview as preview_service
from apps.api.serialization import json_value
from packages.contracts.schemas import Command
from packages.domain.finding_identity import finding_index
from packages.domain import finding_causes, validation
from packages.domain.validation import check_contracts, evaluate
from .clock_support import freeze_clock
from .test_backend_reads_scale import read_department
from .test_backend_scale import measure
from .test_catalogue_preview import apply, preview, state
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
    phase_inputs, reads, reading, captures = [], {'shared': [], 'fresh': [], 'trial': []}, [], []
    capture_phases = []
    catalogue_reads = {'shared': 0, 'fresh': 0}
    project_catalogue = snapshots._snapshot_inputs
    projecting_catalogue = False

    def sql_read(conn, cursor, statement, parameters, context, executemany):
        if reading and statement.lstrip().upper().startswith(('SELECT', 'WITH')):
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

    replay_connections = []

    def inputs(db, org):
        if phase_inputs and db.get_bind().dialect.name == 'sqlite':
            connection = db.connection()
            event.listen(connection, 'before_cursor_execute', sql_read)
            replay_connections.append(connection)
        reading.append('shared')
        try:
            result = real_inputs(db, org)
        finally:
            reading.pop()
        phase_inputs.append(result)
        return result

    capture_sources = preview_service.capture_catalogue_sources

    def capture(db, *args, **kwargs):
        assert db.connection().info.get('rota_write') and db.in_nested_transaction()
        reading.append('trial')
        try:
            return capture_sources(db, *args, **kwargs)
        finally:
            reading.pop()

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
    monkeypatch.setattr(preview_service, 'capture_catalogue_sources', capture)
    monkeypatch.setattr(preview_service, 'domain_snapshot', snapshot)
    monkeypatch.setattr(preview_service, 'evaluate_preview', compare)
    monkeypatch.setattr(snapshots, '_snapshot_inputs', project)
    event.listen(h.app.state.engine, 'before_cursor_execute', sql_read)
    try:
        result = preview(h, command)
    finally:
        event.remove(h.app.state.engine, 'before_cursor_execute', sql_read)
        for connection in replay_connections:
            event.remove(connection, 'before_cursor_execute', sql_read)
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
    assert capture_phases == [(False, False)] * len(captures)
    assert result['workspace_count'] == len(result['impacts']) == 2
    assert state(h) == unchanged and person(h) == row
    # Read the actual people table once before and once within the trial. The
    # second shared projection scans the captured people, with no live fallback.
    scans = {mode: sum('FROM people' in sql for sql in statements) for mode, statements in reads.items()}
    assert scans == {'shared': 2, 'fresh': len(captures), 'trial': 1}
    # The fresh oracle uses the same sessions after the real snapshots, so its
    # baseline and other session-local reads are already warm. Count catalogue projection work.
    assert catalogue_reads['shared'] < catalogue_reads['fresh']
    print(json.dumps({'operation': operation, 'published': published, 'people_scans': scans,
                      'catalogue_selects': catalogue_reads,
                      'selects': {mode: len(statements) for mode, statements in reads.items()}}))


# The default fixture keeps the original SQLite node ids; this runs the same assertions on PostgreSQL.
@pytest.mark.parametrize('h', [pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
@pytest.mark.parametrize('published', [False, True])
@pytest.mark.parametrize('operation', ['create', 'archive'])
def test_phase_batched_inputs_match_fresh_snapshots_on_postgresql(h, monkeypatch, published, operation):
    test_phase_batched_inputs_match_fresh_snapshots_and_reduce_catalogue_queries(h, monkeypatch, published, operation)


def test_hours_reuse_keeps_counted_and_focal_intervals_distinct():
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    early = ({}, {}, start, start + timedelta(hours=4))
    late = ({}, {}, start + timedelta(days=1), start + timedelta(days=1, hours=6))
    data = {'organisation': {'week_start': 0}}
    rule = {'kind': 'max_hours', 'window': 'day'}
    cache = {}
    observations = validation._max_hours_observations
    assert [hours for _, _, hours in observations(data, rule, [early], [early], 'UTC', cache)] == [4]
    assert [hours for _, _, hours in observations(data, rule, [early, late], [late], 'UTC', cache)] == [6]
    assert [hours for _, _, hours in observations(data, rule, [early, late], [early], 'UTC', cache)] == [4]


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_create_impact_reuses_workload_calculations_at_department_scale(scale_harness, monkeypatch, backend):
    freeze_clock(monkeypatch, datetime(2026, 10, 15, 12, tzinfo=timezone.utc))
    h = scale_harness(backend)
    read_department(h)
    h.client('admin')
    evaluate_preview = preview_service.evaluate_preview
    workload_actual = validation.workload_actual
    prepared_checks = []
    hours = 0

    def count(rule, *args):
        nonlocal hours
        hours += rule['kind'] == 'max_hours'
        return workload_actual(rule, *args)

    def capture(prepared):
        prepared_checks.append(prepared)
        return evaluate_preview(prepared)

    monkeypatch.setattr(validation, 'workload_actual', count)
    monkeypatch.setattr(preview_service, 'evaluate_preview', capture)
    for catalogue, value in (
            ('people', {'display_name': 'Fictional new person'}),
            ('categories', {'name': 'Fictional new rank', 'starts_on': '2020-01-01'})):
        command = {'kind': 'catalogue.create', 'payload': {'catalogue': catalogue,
                   'value': value, 'reason': 'Preview a synthetic catalogue addition'}}
        unchanged = state(h)
        hours = 0
        result, _ = measure(h, f'{catalogue} impact preview', lambda: preview(h, command))
        reused_hours = hours
        assert state(h) == unchanged
        prepared = prepared_checks.pop()
        [workspace] = prepared['workspaces']
        assert len(workspace['before']['focus_ids']) == 768
        assert 'input_manifest' not in workspace['before'] and 'input_manifest' not in workspace['after']
        assert workspace['saved']['fingerprint'] == result['impacts'][0]['saved_draft_validation']['fingerprint']

        hours = 0
        assert result == explained_oracle(prepared)
        assert 0 < reused_hours < hours, (catalogue, reused_hours, hours)
        print(catalogue, {'reused_hours_calculations': reused_hours, 'original_hours_calculations': hours})

        committed, _ = measure(h, f'{catalogue} save', lambda: apply(h, command, result['source_fingerprint']))
        assert committed['record']['id']


def spread_department(h):
    """The standard department with its duty rows spread across forty forward rotas."""
    engine = h.app.state.engine
    source = read_department(h)
    with transaction(engine, write=True) as db:
        first = db.get(m.Workspace, source['id'])
        workspaces = [m.Workspace(department_id=first.department_id, name=f'Impact rota {i:02}',
            starts_on=first.starts_on, ends_on=first.ends_on, created_by=first.created_by,
            last_edited_by=first.last_edited_by) for i in range(39)]
        db.add_all(workspaces)
        db.flush()
        workspaces.append(first)
        shifts = list(db.scalars(select(m.Shift).where(m.Shift.workspace_id == first.id).order_by(m.Shift.start, m.Shift.id)))
        for index, shift in enumerate(shifts):
            shift.workspace_id = workspaces[index % len(workspaces)].id
        people = db.scalar(select(func.count()).select_from(m.Membership)
                           .where(m.Membership.department_id == first.department_id))
        rules = db.scalar(select(func.count()).select_from(m.Policy).where(m.Policy.department_id == first.department_id))
        assert (people, len(shifts), rules, len(workspaces)) == (163, 768, 1004, 40)
        return first.id


def _first_json_difference(expected, actual, path=()):
    if type(expected) is not type(actual):
        return path
    if isinstance(expected, dict):
        for key in sorted(expected.keys() | actual.keys()):
            if key not in expected or key not in actual:
                return (*path, key)
            difference = _first_json_difference(expected[key], actual[key], (*path, key))
            if difference is not None:
                return difference
    elif isinstance(expected, list):
        for index, (left, right) in enumerate(zip(expected, actual)):
            difference = _first_json_difference(left, right, (*path, index))
            if difference is not None:
                return difference
        if len(expected) != len(actual):
            return (*path, min(len(expected), len(actual)))
    elif json.dumps(expected) != json.dumps(actual):
        return path
    return None


@pytest.mark.parametrize('backend', ['h', pytest.param('pg', marks=pytest.mark.postgres)])
def test_trial_source_snapshot_bytes_at_department_scale(scale_harness, monkeypatch, tmp_path, backend):
    from apps.api.catalogue_snapshot_sources import capture_catalogue_sources, capture_issued_sources, catalogue_source_reader

    freeze_clock(monkeypatch, datetime(2026, 10, 15, 12, tzinfo=timezone.utc))
    h = scale_harness(backend)
    first = spread_department(h)
    engine = h.app.state.engine
    identity = h.seed['identities']['admin']
    actor = Actor(identity, h.seed['department_id'], h.seed['organisation_id'], 'Synthetic operator',
                  frozenset({'admin', 'coordinator'}), None, 'synthetic')
    programme = h.configure('programmes', {'name': 'Scale programme', 'starts_on': '2020-01-01'})['record']
    other = h.configure('programmes', {'name': 'Other programme', 'starts_on': '2020-01-01'})['record']
    tag = h.configure('tags', {'name': 'Scale audience', 'labels': ['people']})['record']
    old_person, new_person = h.seed['people'][:2]
    membership = h.configure('programme_memberships', {'programme_id': other['id'], 'person_id': old_person,
        'starts_on': '2027-01-01', 'ends_on': '2027-03-01'})['record']
    with transaction(engine, write=True) as db:
        focus = db.get(m.Workspace, first)
        retired = m.Workspace(department_id=actor.department_id, name='Replaced rota', status='superseded',
            starts_on=focus.starts_on, ends_on=focus.ends_on, created_by=identity, last_edited_by=identity,
            validation={'wishes': []})
        db.add(retired)
        db.flush()
        db.add(m.Publication(department_id=actor.department_id, workspace_id=retired.id, current=False,
            sequence=1, starts_on=retired.starts_on, ends_on=retired.ends_on, published_by=identity,
            snapshot={}, validation={'contracts': check_contracts()}, fingerprint='0' * 64))
        db.add(m.StaffWish(department_id=actor.department_id, person_id=old_person, kind='prefer_on',
            first_day=focus.starts_on, last_day=focus.starts_on, points=1, created_by=identity,
            repeat={'weekdays': [focus.starts_on.weekday()], 'starts_on': focus.starts_on.isoformat()}))
        # A legacy Apply receipt derives the population from its predicate and current links.
        db.add(m.Job(workspace_id=first, department_id=actor.department_id, requested_by=identity,
            kind='generation', status='applied', input_fingerprint='0' * 64, input_snapshot={},
            read_metadata={'input_snapshot': {'generation_scope': {'conditions': {'match': 'all', 'conditions': [
                {'field': 'person_ids', 'op': 'any', 'values': [tag['id']]}]}}}, 'result': {}}))

    cases = {
        'create': Command(kind='catalogue.create', payload={'catalogue': 'people',
            'value': {'display_name': 'Scale trial person', 'tags': ['Scale audience']}, 'reason': 'Preview creation'}),
        'move': Command(kind='catalogue.update', target_id=membership['id'], expected_revision=membership['revision'],
            payload={'catalogue': 'programme_memberships', 'reason': 'Preview dated change', 'value': {
                'programme_id': programme['id'], 'person_id': new_person, 'starts_on': '2026-10-16', 'ends_on': '2026-11-01'}}),
        'link': Command(kind='catalogue.update', target_id=tag['id'], expected_revision=tag['revision'],
            payload={'catalogue': 'tags', 'reason': 'Preview audience change', 'value': {'name': tag['name'], 'labels': ['people']},
                     'members': {'add': [{'kind': 'people', 'id': old_person}]}}),
    }

    def snapshot_bytes(db, w, shared, *, operational):
        db.flush()
        snapshot = snapshots._domain_snapshot(db, w, operational=operational, shared_inputs=shared,
            mandatory_rules=operational)
        encoded = json.dumps((snapshot, getattr(snapshot, 'read_quality', None)),
                             sort_keys=True, separators=(',', ':')).encode()
        return encoded, snapshot.get('applied_planning_population'), len(snapshot['wishes'])

    for case, command in cases.items():
        with transaction(engine, write=True) as db:
            revision = db.get(m.Organisation, actor.organisation_id).input_revision
            workspaces = preview_service.candidates(db, actor)
            assert len(workspaces) == 40
            trial = db.begin_nested()
            try:
                issued = capture_issued_sources(db, actor.organisation_id, workspaces)
                preview_service.write_catalogue(db, actor, command, update=case != 'create', preview_only=True)
                captured = capture_catalogue_sources(db, actor.organisation_id, workspaces,
                                                     captured_at=m.now(), issued_sources=issued, before_baselines={})
                proposed = snapshots.snapshot_inputs(db, db.get(m.Organisation, actor.organisation_id))
                assert proposed['organisation']['input_revision'] > revision
                expected = {}
                for index, w in enumerate(workspaces):
                    for operational in (True, False):
                        encoded, population, wishes = snapshot_bytes(db, w, proposed, operational=operational)
                        oracle = tmp_path / f'{index}-{operational}.json'
                        oracle.write_bytes(encoded)
                        expected[w.id, operational] = hashlib.sha256(encoded).hexdigest(), population, wishes, oracle
            finally:
                trial.rollback()
            assert db.get(m.Organisation, actor.organisation_id).input_revision == revision
        with catalogue_source_reader(engine, captured, actor) as (db, _):
            detached_org = db.get(m.Organisation, actor.organisation_id)
            shared = snapshots.snapshot_inputs(db, detached_org)
            assert shared['organisation']['input_revision'] == proposed['organisation']['input_revision']
            for (ident, operational), value in expected.items():
                encoded, population, wishes = snapshot_bytes(db, db.get(m.Workspace, ident), shared, operational=operational)
                assert (hashlib.sha256(encoded).hexdigest(), population, wishes) == value[:3], (
                    case, ident, operational, 'first differing JSON path',
                    _first_json_difference(json.loads(value[3].read_bytes()), json.loads(encoded)))
        if case == 'link':
            assert old_person in expected[first, True][1]
        # A superseded publication must not masquerade as an unissued settled rota and close these wish days.
        assert expected[first, True][2] > 0
