"""Participant edits leave other canonical assignment rows completely untouched."""
from contextlib import contextmanager
from copy import deepcopy

import pytest
from sqlalchemy import event, select
from ortools.sat.python import cp_model

from apps.api import models as m
from apps.api.db import transaction
from packages.contracts import schemas as s
from .test_generation_apply_scale import completed_proposal
from .test_governed_leadership import activate, line, proposal
from .test_rule_catalogue_api import approve, propose, line as rule_line, person
from .test_series_reconciliation import template, materialise, amend_template, reconcile


pytestmark = pytest.mark.parametrize('h', ['sqlite', pytest.param('postgresql', marks=pytest.mark.postgres)], indirect=True)
START, END = '2026-10-04T08:00:00+03:00', '2026-10-04T12:00:00+03:00'


@pytest.fixture(autouse=True)
def no_solver(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Assignment identity tests must not run CP-SAT')
    monkeypatch.setattr(cp_model.CpSolver, 'solve', forbidden)


def participant(pid, **extra):
    return {'person_id': pid, 'start': START, 'end': END, **extra}


def payload(workspace, shift):
    return {'workspace_id': workspace['id'], 'shift_id': shift['id'],
            **{key: deepcopy(shift[key]) for key in ('assignment_type_id', 'location_id', 'service_ids', 'start', 'end', 'attributes')},
            'participants': [{key: deepcopy(p[key]) for key in s.ParticipantInput.model_fields} for p in shift['participants']]}


def stored(h, sid):
    with transaction(h.app.state.engine) as db:
        return {row['person_id']: dict(row) for row in db.execute(
            select(m.Assignment.__table__).where(m.Assignment.shift_id == sid)).mappings()}


@contextmanager
def assignment_writes(h):
    writes = []
    def capture(connection, cursor, statement, parameters, context, executemany):
        if context.compiled is not None and context.compiled.statement.is_dml and context.compiled.statement.table.name == 'assignments':
            writes.append((statement, deepcopy(context.compiled_parameters), cursor.rowcount))
    event.listen(h.app.state.engine, 'after_cursor_execute', capture)
    try:
        yield writes
    finally:
        event.remove(h.app.state.engine, 'after_cursor_execute', capture)


def assert_writes(writes, deleted=(), inserted=()):
    """Inspect executed DML, including bound IDs, so identity coercion cannot satisfy this test."""
    deletes, inserts = [], []
    for sql, parameters, count in writes:
        verb = sql.split()[0].upper()
        assert verb != 'UPDATE', (sql, parameters)
        if verb == 'DELETE':
            assert 'assignments.id IN (' in sql, sql
            deletes.extend(pid for values in parameters for pid in values.values())
            assert count == len(deleted)
        else:
            assert verb == 'INSERT', sql
            inserts.extend(values['person_id'] for values in parameters)
    # Staged commands repeat their DML in rolled-back preparation passes (dca06b33a), as bulk apply does in its
    # preview savepoint: every pass must delete exactly these rows and insert only changed people.
    assert set(deletes) == set(deleted)
    assert set(inserts) == set(inserted)


def roster(h, *, visible=True):
    supervisor, trainee, colleague = h.seed['people']
    activate(h, proposal(h, [line(h.seed['type'], [supervisor])]))
    people = [participant(supervisor, participation='supervisor', team_lead=True, fixed=True),
              participant(trainee, participation='trainee', supervisor_person_id=supervisor, fixed=True,
                  segments=[{'start': START, 'end': '2026-10-04T10:00:00+03:00'},
                            {'start': '2026-10-04T10:30:00+03:00', 'end': END}])]
    if visible:
        people.append(participant(colleague))
    workspace = h.shift(h.workspace(), people=people)['workspace']
    return workspace, h.detail(workspace)['shifts'][0]


@pytest.mark.parametrize('action', ['remove', 'edit', 'add'])
def test_visible_person_mutation_preserves_full_hidden_rows_and_exact_undo(h, action):
    workspace, shift = roster(h, visible=action != 'add')
    original = stored(h, shift['id'])
    value = payload(workspace, shift)
    visible = h.seed['people'][2]
    if action == 'remove':
        value['participants'] = [p for p in value['participants'] if p['person_id'] != visible]
    elif action == 'edit':
        next(p for p in value['participants'] if p['person_id'] == visible)['overtime'] = True
    else:
        value['participants'].append(participant(visible))
    # Omission must retain a hidden person's fixed flag, rather than normalize it to false.
    for p in value['participants']:
        if p['person_id'] != visible:
            p.pop('fixed')
    with assignment_writes(h) as writes:
        result = h.command('coordinator', 'shift.update', value, revision=workspace['revision'])
    current = stored(h, shift['id'])
    after = h.detail(workspace)['shifts'][0]
    for pid in h.seed['people'][:2]:
        assert current[pid] == original[pid]
        assert next(p for p in after['participants'] if p['person_id'] == pid) == next(
            p for p in shift['participants'] if p['person_id'] == pid)
    assert_writes(writes, deleted=[original[visible]['id']] if action != 'add' else [],
                  inserted=[visible] if action != 'remove' else [])
    if action == 'edit':
        assert current[visible]['id'] != original[visible]['id']
        assert current[visible]['overtime'] and current[visible]['source'] == 'manual'
    h.command('coordinator', 'workspace.undo', {'reason': 'Restore exact original assignment rows'},
              workspace['id'], result['workspace']['revision'])
    assert stored(h, shift['id']) == original
    assert h.detail(workspace)['shifts'] == [shift]


def test_generated_source_is_retained_only_for_unchanged_content(h):
    workspace = h.shift(h.workspace(), people=[])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    pid = h.seed['people'][0]
    job = completed_proposal(h, workspace, [{'shift_id': shift['id'], 'participant': participant(pid)}])
    workspace = h.command('coordinator', 'generation.apply', {'workspace_id': workspace['id'], 'job_id': job['id'],
        'reason': 'Synthetic proposal without executing a solver'}, revision=workspace['revision'])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    original = stored(h, shift['id'])
    assert original[pid]['source_run_id'] == job['id']
    value = payload(workspace, shift)
    value['participants'].append(participant(h.seed['people'][2]))
    with assignment_writes(h) as writes:
        workspace = h.command('coordinator', 'shift.update', value, revision=workspace['revision'])['workspace']
    assert stored(h, shift['id'])[pid] == original[pid]
    assert_writes(writes, inserted=[h.seed['people'][2]])
    shift = h.detail(workspace)['shifts'][0]
    value = payload(workspace, shift)
    next(p for p in value['participants'] if p['person_id'] == pid)['overtime'] = True
    with assignment_writes(h) as writes:
        result = h.command('coordinator', 'shift.update', value, revision=workspace['revision'])
    changed = stored(h, shift['id'])[pid]
    assert changed['id'] != original[pid]['id']
    assert changed['source'] == 'manual' and changed['source_run_id'] is None
    assert_writes(writes, deleted=[original[pid]['id']], inserted=[pid])
    h.command('coordinator', 'workspace.undo', {'reason': 'Restore generated source evidence'},
              workspace['id'], result['workspace']['revision'])
    assert stored(h, shift['id'])[pid] == original[pid]


def test_flag_change_cannot_reuse_an_old_override_decision(h):
    pid, other = h.seed['people'][:2]
    approve(h, propose(h, 'duties_per_month', [rule_line(who=person(pid), numbers={'max': {'value': 0, 'level': 'mandatory'}})])['record'])
    workspace = h.shift(h.workspace(), people=[pid, other])['workspace']
    shift = h.detail(workspace)['shifts'][0]
    original = stored(h, shift['id'])
    assert original[pid]['override_reason']
    value = payload(workspace, shift)
    next(p for p in value['participants'] if p['person_id'] == pid)['overtime'] = True
    with assignment_writes(h) as writes:
        refused = h.command('coordinator', 'shift.update', value, revision=workspace['revision'], status=422)
    assert refused['error']['code'] == 'PERSON_UNAVAILABLE'
    assert not writes and stored(h, shift['id']) == original
    value['override_reason'] = 'New reviewed decision for overtime during a zero duty limit'
    with assignment_writes(h) as writes:
        result = h.command('coordinator', 'shift.update', value, revision=workspace['revision'])
    current = stored(h, shift['id'])
    assert current[other] == original[other]
    assert current[pid]['override_reason'] == value['override_reason'] and current[pid]['id'] != original[pid]['id']
    assert_writes(writes, deleted=[original[pid]['id']], inserted=[pid])
    assert any(f['code'] == 'QUOTA_ABOVE_MAXIMUM' for f in result['validation']['findings'])
    assert h.lifecycle(result['workspace'], 'submit', status=422)['error']['code'] == 'VALIDATION_FAILED'


@pytest.mark.parametrize('guard', ['stale', 'authority', 'locked', 'partial'])
def test_identity_repair_keeps_write_guards(h, guard):
    workspace = h.shift(h.workspace())['workspace']
    shift = h.detail(workspace)['shifts'][0]
    original = stored(h, shift['id'])
    value, role, revision, status = payload(workspace, shift), 'coordinator', workspace['revision'], 409
    if guard == 'stale':
        revision -= 1
    elif guard == 'authority':
        role, status = 'staff', 403
    elif guard == 'partial':
        status = 422
        value['participants'][0]['end'] = '2026-10-04T11:00:00+03:00'
    else:
        workspace = h.command('coordinator', 'shift.action', {'workspace_id': workspace['id'], 'shift_ids': [shift['id']],
            'action': 'lock', 'reason': 'Protect the reviewed assignment'}, revision=revision)['workspace']
        revision = workspace['revision']
    with assignment_writes(h) as writes:
        h.command(role, 'shift.update', value, revision=revision, status=status)
    assert not writes and stored(h, shift['id']) == original


def test_batch_recurring_reconcile_and_copy_keep_existing_contracts(h):
    series = template(h, days=(6,), definition={'anchor': '2026-10-04',
        'days': [{'weekday': 6, 'start_time': '08:00', 'end_time': '12:00'}],
        'service_ids': [h.seed['services'][0]], 'default_person_ids': h.seed['people'][:2]})
    workspace = materialise(h, series)
    shift = h.detail(workspace)['shifts'][0]
    original = stored(h, shift['id'])
    assert all(p['source'] == 'recurring' for p in original.values())
    value = payload(workspace, shift)
    value['participants'].append(participant(h.seed['people'][2]))
    with assignment_writes(h) as writes:
        bulk = h.command('coordinator', 'bulk.apply', {'item_kind': 'shift.update',
            'items': [{'expected_revision': workspace['revision'], 'payload': value}], 'reason': 'Add a colleague in one batch'})
    assert_writes(writes, inserted=[h.seed['people'][2]])
    for pid, row in original.items():
        assert stored(h, shift['id'])[pid] == row
    h.command('coordinator', **bulk['undo'])
    workspace = h.detail(workspace)['workspace']
    assert stored(h, shift['id']) == original
    # Reconciliation may change session context while retaining identical participant fields and provenance.
    series = amend_template(h, series, start='08:00', end='12:00')
    with assignment_writes(h) as writes:
        result = reconcile(h, workspace, series, from_on='2026-10-04', replace_modified=True)
    assert not writes and stored(h, shift['id']) == original
    workspace = result['workspace']
    copied = h.command('coordinator', 'shift.copy', {'workspace_id': workspace['id'], 'shift_ids': [shift['id']],
        'offset_days': 1, 'reason': 'Copy with fresh identities and manual provenance'}, revision=workspace['revision'])
    assert stored(h, shift['id']) == original
    clone = next(row for row in copied['shifts'] if row['id'] != shift['id'])
    assert all(p['source'] == 'manual' and p['fixed'] for p in clone['participants'])
    assert not {p['id'] for p in clone['participants']} & {p['id'] for p in original.values()}
