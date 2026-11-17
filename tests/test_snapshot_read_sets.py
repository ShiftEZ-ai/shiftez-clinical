"""Snapshot read declarations cover consumed fields and conditional write paths."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, event as sql_event, insert, literal, select, update
from sqlalchemy.sql import visitors

from apps.api import models as m, snapshots
from apps.api.db import snapshot_input_listener_classes, transaction
from packages.contracts.schemas import GENERATION_RUNTIME_OPTIONS
from apps.api.services import audiences, availability_patterns
from tests.test_events import event, event_type, input_revisions
from tests.test_fix_list_performance import backend
from tests.test_request_decide_many import request_row
from tests.test_events_access_fingerprint import test_event_attendance_access_change_stales_only_blocking_generation as check_event_access


class ReadRow:
    def __init__(self, **values):
        self.values, self.read = values, set()

    def __getattr__(self, name):
        self.read.add(name)
        return self.values[name]


def test_declared_snapshot_read_sets_match_consumed_attributes():
    """Execute the actual readers; adding a consumed column must change its declaration."""
    specs = {spec[1]: spec for spec in snapshots.snapshot_input_specs()}
    class ReadMapping(dict):
        def __init__(self, **values):
            super().__init__(values)
            self.read = set()

        def __getitem__(self, key):
            self.read.add(key)
            return super().__getitem__(key)

        def get(self, key, default=None):
            self.read.add(key)
            return super().get(key, default)

    department = ReadMapping(id='department', organisation_id='organisation', operating_calendar_id=None,
        weekend_days=[5, 6], generation_defaults={}, wish_settings={})
    specs[m.Department][4].projection(department)
    assert specs[m.Department][4].columns == department.read
    assert specs[m.Department][-1] == (set(m.Department.__table__.columns.keys()) - department.read) | {
        'generation_defaults.' + key for key in GENERATION_RUNTIME_OPTIONS}
    workspace = ReadMapping(**dict.fromkeys(m.Workspace.__table__.columns.keys()))
    _, _, _, scope, workspace_shape, _ = specs[m.Workspace]
    workspace_shape.projection(workspace)
    assert scope == 'workspace' and workspace_shape.columns == workspace.read
    assert 'content_revision' not in workspace.read
    binding = ReadRow(identity_id='identity', department_id='department', role='staff',
                      revoked=False, starts_on=date(2020, 1, 1), ends_on=date(2030, 1, 1))
    statements = []

    class DB:
        def scalars(self, statement):
            statements.append(statement)
            return [binding]

        def execute(self, statement):
            statements.append(statement)
            return [('identity', 'person')]

    context = audiences.person_role_context(DB(), 'department', ['person'])
    assert audiences.person_roles(DB(), 'department', ['person'], date(2026, 10, 4), context=context) == {'person': {'staff'}}
    assert specs[m.RoleBinding][4].columns == binding.read
    identity_columns = {node.key for statement in statements for node in visitors.iterate(statement)
                        if getattr(getattr(node, 'table', None), 'name', None) == m.Identity.__tablename__}
    assert specs[m.Identity][4].columns == identity_columns

    pattern = ReadRow(id='pattern', department_id='department', person_id='person',
        weekdays=[6], start_time='09:00', end_time='10:00', starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1))
    lo = datetime(2026, 10, 4, tzinfo=timezone.utc)
    assert availability_patterns.expand(None, ['department'], ['person'], lo, lo + timedelta(days=1), 'UTC', acquired=[pattern])
    cls, where = availability_patterns.snapshot_source(['department'], ['person'])
    predicate_columns = {node.key for node in visitors.iterate(where) if getattr(node, 'table', None) is cls.__table__}
    assert specs[cls][4].columns == pattern.read | predicate_columns
    for cls in (m.Identity, m.RoleBinding, m.AvailabilityPattern):
        assert specs[cls][-1] == set(cls.__table__.columns.keys()) - specs[cls][4].columns


def test_historical_acquisition_has_the_expected_revision_owners():
    owned = {m.Workspace.__table__}
    while linked := {table for table in m.Base.metadata.tables.values() if table not in owned
                     and any(key.column.table in owned for key in table.foreign_keys)}:
        owned.update(linked)
    workspace_tables = {m.Assignment, m.ShiftService, m.Shift, m.Job, m.WorkspaceAccessVersion, m.WorkspaceBaseline}
    assert {cls.__table__ for cls in workspace_tables} <= owned

    listeners = snapshot_input_listener_classes()
    ordinal_sources = {node.name for cls in listeners if issubclass(cls, m.InsertionOrdered)
                       for node in visitors.iterate(cls.seq.__clause_element__())
                       if node is m.Base.metadata.tables['record_insertions']}
    assert m.Base.metadata.tables['record_insertions'] not in owned
    assert ordinal_sources == {'record_insertions'}
    assert m.DefinitionHistory.__table__ not in owned
    assert listeners[m.DefinitionHistory] == 'general'
    _, _, _, scope, shape, _ = next(spec for spec in snapshots.snapshot_input_specs() if spec[1] is m.DefinitionHistory)
    assert scope == 'department' and shape.insert_only


def test_saving_a_draft_check_leaves_it_current(backend):
    h = backend
    workspace = h.shift(h.workspace())['workspace']
    h.lifecycle(workspace, 'validate')
    result = h.detail(workspace)
    assert result['workspace']['content_revision'] == workspace['content_revision']
    assert result['workspace']['validation']['fingerprint'] == result['current_fingerprint']
    assert result['current_fingerprint'] == h.snapshot(workspace)['fingerprint']


def test_loading_an_existing_checked_draft_preserves_its_currency(backend, monkeypatch):
    from apps.api.services.workspace_reads import stored_checks

    h = backend
    with monkeypatch.context() as previous:
        previous.setattr(snapshots, 'SNAPSHOT_INPUT_SOURCE_SPECS', tuple(
            (name, cls, category, 'department', replace(shape, columns=frozenset(), projection=None), exclusions)
            if cls is m.Workspace else (name, cls, category, scope, shape, exclusions)
            for name, cls, category, scope, shape, exclusions in snapshots.snapshot_input_specs()))
        workspace = h.shift(h.workspace())['workspace']
        revisions = input_revisions(h)
        with transaction(h.app.state.engine) as db:
            row = db.get(m.Workspace, workspace['id'])
            check = stored_checks(db, [row])[row.id]
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace['id'])
        assert check['input_manifest']['dirty_hint']['content_revision'] == workspace['content_revision']
        assert snapshots.cheap_fingerprints(db, [row], checks={row.id: check}) == {
            row.id: check['input_manifest']['token']}
    result = h.detail(workspace)
    assert result['workspace']['content_revision'] == workspace['content_revision']
    assert result['current_fingerprint'] == check['input_manifest']['token']
    assert input_revisions(h) == revisions


def test_workspace_statement_stamps_changed_rows_and_preserves_explicit_revision(backend):
    h = backend
    first, second = h.workspace(), h.workspace()
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(update(m.Workspace).where(m.Workspace.id == first['id'])
                   .ordered_values((m.Workspace.origin, 'auto')))
        db.execute(update(m.Workspace), [
            {'id': first['id'], 'origin': 'auto'},
            {'id': second['id'], 'origin': 'auto', 'content_revision': second['content_revision'] + 2},
        ])
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, first['id']).content_revision == first['content_revision'] + 1
        assert db.get(m.Workspace, second['id']).content_revision == second['content_revision'] + 2


@pytest.mark.parametrize('kind', ['policy', 'bulk_staged', 'catalogue_conversion'])
def test_history_insert_stamps_only_historical_snapshot_inputs(backend, kind):
    from copy import deepcopy

    h = backend
    policy = h.policy({'kind': 'min_rest', 'hours': 1})
    published = h.publish(h.shift(h.workspace())['workspace'])
    workspace = published['workspace']
    with transaction(h.app.state.engine) as db:
        publication = db.get(m.Publication, published['publication_id'])
        created_at = publication.created_at
        source = db.get(m.Policy, policy['id'])
        definition = deepcopy(source.definition)

    before = h.snapshot(workspace)['fingerprint']
    revisions = input_revisions(h)
    for index, hours in enumerate((2, 3), start=1):
        definition['rule']['hours'] = hours
        changed = {**policy, 'revision': policy['revision'] + index, 'definition': definition}
        if kind == 'catalogue_conversion':
            changed['status'] = 'proposed'
        with transaction(h.app.state.engine, write=True) as db:
            db.execute(insert(m.DefinitionHistory.__table__).values(
                department_id=h.seed['department_id'], kind=kind, object_id=policy['id'],
                object_revision=changed['revision'], snapshot=changed, actor_id=h.seed['identities']['approver'],
                reason='Synthetic history append', created_at=created_at - timedelta(microseconds=3-index)))
        after = h.snapshot(workspace)['fingerprint']
        current = input_revisions(h)
        assert (after != before) == (kind == 'policy')
        assert current == (revisions[0] + (kind == 'policy'), revisions[1])
        before, revisions = after, current


@pytest.mark.parametrize('writer', ['flush', 'update', 'core', 'bulk', 'delete', 'insert', 'insert_select'])
@pytest.mark.parametrize('active', [False, True])
def test_pattern_read_fields_stamp_only_active_rows(backend, writer, active):
    h = backend
    values = dict(id=m.uid(), department_id=h.seed['department_id'], person_id=h.seed['people'][0],
        weekdays=[6], start_time='09:00', end_time='10:00', starts_on=date(2026, 10, 1),
        status='active' if active else 'proposed', created_by=h.seed['identities']['staff'])
    if writer not in {'insert', 'insert_select'}:
        with transaction(h.app.state.engine, write=True) as db:
            db.add(m.AvailabilityPattern(**values))
    before = input_revisions(h)
    with transaction(h.app.state.engine, write=True) as db:
        cls = m.AvailabilityPattern
        row = db.get(cls, values['id']) if writer not in {'insert', 'insert_select'} else None
        if writer == 'insert':
            db.execute(insert(cls), [values])
        elif writer == 'insert_select':
            from sqlalchemy import JSON
            projected = [literal(value, type_=JSON) if key == 'weekdays' else literal(value) for key, value in values.items()]
            db.execute(insert(cls).from_select(list(values), select(*projected)))
        elif writer == 'delete':
            db.execute(delete(cls).where(cls.id == row.id))
        elif writer == 'flush':
            row.end_time = '11:00'
        elif writer == 'bulk':
            db.execute(update(cls), [{'id': row.id, 'end_time': '11:00'}])
        elif writer == 'core':
            db.execute(update(cls.__table__).where(cls.id == row.id).values(end_time='11:00'))
        else:
            db.execute(update(cls).where(cls.id == row.id).values(end_time='11:00'))
    assert input_revisions(h) == tuple(value + active for value in before)


@pytest.mark.parametrize('writer', ['flush', 'update', 'bulk', 'expression', 'deferred'])
def test_pattern_leaving_read_scope_still_stamps(backend, writer):
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        row = m.AvailabilityPattern(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
            weekdays=[6], start_time='09:00', end_time='10:00', starts_on=date(2026, 10, 1),
            status='active', created_by=h.seed['identities']['staff'])
        db.add(row)
        db.flush()
        ident = row.id
    before = input_revisions(h)
    with transaction(h.app.state.engine, write=True) as db:
        if writer == 'bulk':
            db.execute(update(m.AvailabilityPattern), [{'id': ident, 'status': 'declined'}])
        elif writer in {'update', 'expression'}:
            status = select(literal('declined')).scalar_subquery() if writer == 'expression' else 'declined'
            db.execute(update(m.AvailabilityPattern).where(m.AvailabilityPattern.id == ident).values(status=status))
        else:
            row = db.get(m.AvailabilityPattern, ident)
            if writer == 'deferred':
                db.expire(row, ['status'])
            row.status = 'declined'
    assert input_revisions(h) == tuple(value + 1 for value in before)


@pytest.mark.parametrize('writer', ['flush', 'update'])
def test_pattern_expression_entering_read_scope_still_stamps(backend, writer):
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        row = m.AvailabilityPattern(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
            weekdays=[6], start_time='09:00', end_time='10:00', starts_on=date(2026, 10, 1),
            status='proposed', created_by=h.seed['identities']['staff'])
        db.add(row)
        db.flush()
        ident = row.id
    before = input_revisions(h)
    with transaction(h.app.state.engine, write=True) as db:
        if writer == 'flush':
            db.get(m.AvailabilityPattern, ident).status = literal('active')
        else:
            db.execute(update(m.AvailabilityPattern).where(m.AvailabilityPattern.id == ident).values(status=select(literal('active')).scalar_subquery()))
    assert input_revisions(h) == tuple(value + 1 for value in before)


@pytest.mark.parametrize('cls,field,value', [(m.Identity, 'disabled', True), (m.RoleBinding, 'role', 'approver')])
@pytest.mark.parametrize('writer', ['flush', 'update', 'core', 'bulk'])
@pytest.mark.parametrize('blocks', [False, True])
def test_attendance_read_fields_stamp_only_with_blocking_inputs(backend, cls, field, value, writer, blocks):
    h = backend
    event(h, event_type(h, blocks_duties=blocks))
    before = input_revisions(h)
    with transaction(h.app.state.engine, write=True) as db:
        ident = h.seed['identities']['staff']
        row = db.get(cls, ident) if cls is m.Identity else db.scalars(select(cls).where(cls.identity_id == ident, cls.role == 'staff')).one()
        if writer == 'flush':
            setattr(row, field, value)
        elif writer == 'bulk':
            db.execute(update(cls), [{'id': row.id, field: value}])
        else:
            target = cls.__table__ if writer == 'core' else cls
            db.execute(update(target).where((target.c.id if writer == 'core' else cls.id) == row.id).values(**{field: value}))
    assert input_revisions(h) == tuple(revision + blocks for revision in before)


def test_request_workflow_rows_are_not_acquired_by_scheduling_snapshots(backend):
    h = backend
    row = request_row(h)
    workspace = h.workspace()
    tables = set()

    def acquire(_connection, _cursor, _statement, _parameters, context, _many):
        if context.compiled is not None:
            tables.update(node.name for node in visitors.iterate(context.compiled.statement)
                          if isinstance(node, type(m.RequestRecord.__table__)))

    sql_event.listen(h.app.state.engine, 'before_cursor_execute', acquire)
    try:
        h.snapshot(workspace)
    finally:
        sql_event.remove(h.app.state.engine, 'before_cursor_execute', acquire)
    assert m.RequestRecord.__tablename__ not in tables
    assert m.RequestRecord not in snapshots.snapshot_input_classes()
    before = input_revisions(h)
    h.command('approver', 'request.decide', {'decision': 'reject', 'reason': 'Synthetic reviewed refusal'}, row['id'], row['revision'])
    assert input_revisions(h) == before
    approved = request_row(h)
    h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Synthetic approved leave'}, approved['id'], approved['revision'])
    assert input_revisions(h) == tuple(value + 1 for value in before)


@pytest.mark.postgres
@pytest.mark.parametrize('change', ['role.grant', 'role.revoke', 'identity.disable', 'identity.enable', 'account.invite'])
@pytest.mark.parametrize('blocks,ended', [(True, False), (True, True), (False, False), (None, False)],
                         ids=['blocking', 'ended-blocking', 'nonblocking', 'no-events'])
def test_event_access_journey_has_the_same_postgres_assertions(pg, tmp_path, change, blocks, ended):
    check_event_access(pg, tmp_path, change, blocks, ended)


@pytest.mark.parametrize('cls,field,value', [(m.Identity, 'display_name', 'Changed sign-in label'),
    (m.RoleBinding, 'revision', 2), (m.AvailabilityPattern, 'note', 'Changed explanatory note')])
@pytest.mark.parametrize('writer', ['flush', 'update', 'core', 'bulk'])
def test_unread_metadata_never_stamps_with_blocking_inputs(backend, cls, field, value, writer):
    h = backend
    event(h, event_type(h, blocks_duties=True))
    with transaction(h.app.state.engine, write=True) as db:
        if cls is m.AvailabilityPattern:
            row = cls(department_id=h.seed['department_id'], person_id=h.seed['people'][0],
                weekdays=[6], start_time='09:00', end_time='10:00', starts_on=date(2026, 10, 1),
                status='active', created_by=h.seed['identities']['staff'])
            db.add(row)
            db.flush()
        else:
            ident = h.seed['identities']['staff']
            row = db.get(cls, ident) if cls is m.Identity else db.scalars(select(cls).where(cls.identity_id == ident, cls.role == 'staff')).one()
        ident = row.id
    before = input_revisions(h)
    with transaction(h.app.state.engine, write=True) as db:
        if writer == 'flush':
            setattr(db.get(cls, ident), field, value)
        elif writer == 'bulk':
            db.execute(update(cls), [{'id': ident, field: value}])
        else:
            target = cls.__table__ if writer == 'core' else cls
            db.execute(update(target).where((target.c.id if writer == 'core' else cls.id) == ident).values(**{field: value}))
    assert input_revisions(h) == before


@pytest.mark.parametrize('writer', ['flush', 'update', 'core', 'bulk'])
def test_rejection_removing_approved_absence_changes_fingerprint_and_stamps(backend, writer):
    h = backend
    row = request_row(h)
    row = h.command('approver', 'request.decide', {'decision': 'approve', 'reason': 'Synthetic approved leave'}, row['id'], row['revision'])['record']
    workspace = h.workspace()
    before, revisions = h.snapshot(workspace)['fingerprint'], input_revisions(h)
    with transaction(h.app.state.engine, write=True) as db:
        absence = db.scalars(select(m.Availability).where(m.Availability.request_id == row['id'], m.Availability.retracted.is_(False))).one()
        # The public workflow requires Undo before a second decision; exercise their atomic source writes directly.
        db.get(m.RequestRecord, row['id']).status = 'rejected'
        if writer == 'flush':
            absence.retracted = True
        elif writer == 'bulk':
            db.execute(update(m.Availability), [{'id': absence.id, 'retracted': True}])
        else:
            target = m.Availability.__table__ if writer == 'core' else m.Availability
            db.execute(update(target).where((target.c.id if writer == 'core' else target.id) == absence.id).values(retracted=True))
    assert h.snapshot(workspace)['fingerprint'] != before
    assert input_revisions(h) == tuple(value + 1 for value in revisions)


@pytest.mark.parametrize('source', ['empty', 'reservation'])
def test_new_workspace_sources_fingerprint_and_revision_oracle(backend, source):
    from tests.test_standing_wishes import standing
    h = backend
    standing(h, points=2)
    workspace = h.workspace()
    before, revisions = h.snapshot(workspace)['fingerprint'], input_revisions(h)
    if source == 'empty':
        h.workspace('Empty sibling')
    else:
        with transaction(h.app.state.engine, write=True) as db:
            item = db.scalars(select(m.StaffWish).where(m.StaffWish.person_id == h.seed['people'][0])).first()
            sibling = m.Workspace(id=m.uid(), department_id=h.seed['department_id'], name='Sibling wish reservation',
                starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11), status='draft',
                created_by=h.seed['identities']['coordinator'], last_edited_by=h.seed['identities']['coordinator'])
            db.add(sibling)
            db.flush()
            assert db.get(m.Organisation, h.seed['organisation_id']).input_revision == revisions[0]
            db.add(m.WishOverride(department_id=h.seed['department_id'], wish_id=item.id, workspace_id=sibling.id,
                                   skip=True))
    after = h.snapshot(workspace)['fingerprint']
    assert (after != before) == (source == 'reservation')
    assert input_revisions(h) == (revisions[0] + (source == 'reservation'), revisions[1])


@pytest.mark.parametrize('writer', ['flush', 'statement', 'core', 'bulk', 'insert_select'])
def test_empty_workspace_insert_paths_preserve_fingerprint_and_revisions(backend, writer):
    h = backend
    workspace = h.workspace()
    before, revisions = h.snapshot(workspace)['fingerprint'], input_revisions(h)
    for label in ('First empty sibling', 'Second empty sibling'):
        values = dict(id=m.uid(), department_id=h.seed['department_id'], name=label,
            starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11), status='draft',
            created_by=h.seed['identities']['coordinator'], last_edited_by=h.seed['identities']['coordinator'])
        with transaction(h.app.state.engine, write=True) as db:
            if writer == 'flush':
                db.add(m.Workspace(**values))
            elif writer == 'insert_select':
                db.execute(insert(m.Workspace).from_select(list(values), select(*(literal(value) for value in values.values()))))
            elif writer == 'bulk':
                db.execute(insert(m.Workspace), [values])
            else:
                target = m.Workspace.__table__ if writer == 'core' else m.Workspace
                db.execute(insert(target).values(**values))
        assert h.snapshot(workspace)['fingerprint'] == before
        assert input_revisions(h) == revisions


def test_workspace_statement_delete_projects_only_scope_columns(backend):
    h = backend
    with transaction(h.app.state.engine, write=True) as db:
        sibling = m.Workspace(department_id=h.seed['department_id'], name='Synthetic unbound preview',
            starts_on=date(2026, 10, 4), ends_on=date(2026, 10, 11), status='draft',
            created_by=h.seed['identities']['coordinator'], last_edited_by=h.seed['identities']['coordinator'])
        db.add(sibling)
        db.flush()
        ident = sibling.id
    with transaction(h.app.state.engine, write=True) as db:
        db.execute(delete(m.Workspace).where(m.Workspace.id == ident))
    with transaction(h.app.state.engine) as db:
        assert db.get(m.Workspace, ident) is None
