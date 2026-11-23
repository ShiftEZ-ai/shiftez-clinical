"""Freeze the relational reads needed by a catalogue trial before it rolls back."""
import pickle
from contextlib import contextmanager

from sqlalchemy import JSON, Column, MetaData, Table, Text, bindparam, case, cast, create_engine, func, literal, or_, select, tuple_
from sqlalchemy.orm import Session

from . import models as m
from .snapshots import snapshot_department_ids, snapshot_input_specs
from .workspace_baselines import baseline_rows, ranked_baseline_keys, verified_baseline_keys


def _raw_rows(db, cls, predicate, keys=None, *, projected=None, source=None, trailing=()):
    table = cls.__table__
    keys = table.c.keys() if keys is None else keys
    projected = projected or {}
    columns = [projected.get(key, table.c[key]) for key in keys]
    columns = [cast(column, Text).label(key) if isinstance(table.c[key].type, JSON) else column
               for key, column in zip(keys, columns)]
    query = select(*columns, *trailing).where(predicate)
    if source is not None:
        query = query.select_from(source)
    return keys, [tuple(row) for row in db.execute(query)]


def _baseline_source_row(row):
    return tuple(getattr(row, key) for key in m.WorkspaceBaseline.__table__.c.keys())


def capture_verified_baselines(db):
    keys = verified_baseline_keys(db)
    return {(row.id, row.payload_hash): _baseline_source_row(row)
            for row in baseline_rows(db).values() if row is not None and (row.id, row.payload_hash) in keys}


def _publication_scope(department_ids, workspace_ids, base_ids):
    return or_(m.Publication.department_id.in_(department_ids) & m.Publication.current.is_(True),
               m.Publication.workspace_id.in_(workspace_ids), m.Publication.id.in_(base_ids))


def capture_issued_sources(db, organisation_id, workspaces):
    """Copy issued evidence in the before reader. A catalogue trial cannot change it."""
    workspace_ids = [w.id for w in workspaces]
    departments = snapshot_department_ids(db, organisation_id)
    preview_departments = {w.department_id for w in workspaces}
    scope = _publication_scope(departments, workspace_ids, [w.base_publication_id for w in workspaces if w.base_publication_id])
    external = select(m.Publication.workspace_id).where(scope)
    external_shifts = m.Shift.workspace_id.in_(external) & m.Shift.workspace_id.not_in(workspace_ids)
    shifts = select(m.Shift.id).where(external_shifts)
    # Superseded publications still join to wish reservations; only the join fields are needed.
    return {
        m.Publication.__tablename__: _raw_rows(db, m.Publication, m.Publication.department_id.in_(preview_departments) | scope,
            projected={key: case((scope, m.Publication.__table__.c[key])) for key in ('snapshot', 'validation')}),
        m.Shift.__tablename__: _raw_rows(db, m.Shift, external_shifts),
        m.ShiftService.__tablename__: _raw_rows(db, m.ShiftService, m.ShiftService.shift_id.in_(shifts)),
        m.Assignment.__tablename__: _raw_rows(db, m.Assignment, m.Assignment.shift_id.in_(shifts)),
    }


def capture_catalogue_sources(db, organisation_id, workspaces, *, captured_at, issued_sources, before_baselines):
    """Capture rows once for the batch; query and project them after the writer exits."""
    db.flush()
    workspace_ids = [w.id for w in workspaces]
    preview_departments = sorted({w.department_id for w in workspaces})
    department_ids = snapshot_department_ids(db, organisation_id)
    person_keys, person_rows = _raw_rows(db, m.Person, m.Person.organisation_id == organisation_id,
                                       projected={'pager': literal(None).label('pager')})
    person_id_index = person_keys.index('id')
    people = [row[person_id_index] for row in person_rows]
    workspace_scope = m.Workspace.department_id.in_(department_ids)
    archived_by = select(m.Workspace.archived_by).where(m.Workspace.id.in_(workspace_ids))
    publication_scope = _publication_scope(
        department_ids, workspace_ids, [w.base_publication_id for w in workspaces if w.base_publication_id])
    external = select(m.Publication.workspace_id).where(publication_scope)
    own_shifts = m.Shift.workspace_id.in_(workspace_ids)
    own_shift_ids = select(m.Shift.id).where(own_shifts)
    shift_scope = own_shifts | m.Shift.workspace_id.in_(external)
    shifts = select(m.Shift.id).where(shift_scope)
    rotations = select(m.Rotation.id).where(m.Rotation.department_id.in_(department_ids))
    occurrences = select(m.EventOccurrence.id).where(m.EventOccurrence.department_id.in_(department_ids))

    def department(cls):
        return cls.department_id.in_(department_ids)

    wish_scope = m.StaffWish.department_id.in_(preview_departments) & (m.StaffWish.status == 'open')

    history = m.DefinitionHistory
    adjusted = {(row['policy_id'], row.get('policy_revision')) for w in workspaces for row in w.rule_adjustments or ()}
    history_scope = or_(history.kind == 'scheduling_fact',
        (history.kind == 'policy') & tuple_(history.object_id, history.object_revision).in_(adjusted))
    published = [w.id for w in workspaces if w.status == 'published']
    if published:
        latest_issue = select(func.max(m.Publication.created_at)).where(
            m.Publication.workspace_id.in_(published)).scalar_subquery()
        # Old exception receipts can point to retained evidence written after publication.
        history_scope = or_(history_scope, history.kind == 'exception',
            history.kind.in_(('date_sets', 'policy', 'catalogue_conversion')) & (history.created_at <= latest_issue))

    sources = {cls: department(cls) for _, cls, _, scope, shape, _ in snapshot_input_specs()
               if scope == 'department' and shape is not None}
    sources.update({
        m.Organisation: m.Organisation.id == organisation_id,
        m.Department: m.Department.id.in_(department_ids),
        m.Rotation: department(m.Rotation),
        m.RotationAttachment: m.RotationAttachment.rotation_id.in_(rotations)
        | m.RotationAttachment.mentorship_id.in_(select(m.Mentorship.id).where(department(m.Mentorship))),
        m.Workspace: workspace_scope | m.Workspace.id.in_(external),
        m.WorkspaceAccessVersion: m.WorkspaceAccessVersion.id.in_(select(m.Workspace.access_version_id).where(
            workspace_scope | m.Workspace.id.in_(external))),
        m.PlanningGroupVersion: department(m.PlanningGroupVersion),
        m.Shift: own_shifts,
        m.ShiftService: m.ShiftService.shift_id.in_(own_shift_ids),
        m.Assignment: m.Assignment.shift_id.in_(own_shift_ids),
        m.AvailabilityPattern: department(m.AvailabilityPattern) & (m.AvailabilityPattern.status == 'active'),
        m.Event: department(m.Event) & m.Event.cancelled.is_(False),
        m.EventOccurrence: m.EventOccurrence.id.in_(occurrences),
        m.AnnouncementReceipt: m.AnnouncementReceipt.event_occurrence_id.in_(occurrences)
        & (m.AnnouncementReceipt.response == 'going'),
        m.RoleBinding: department(m.RoleBinding),
        m.Identity: (m.Identity.person_id.in_(people) & m.Identity.disabled.is_(False)) | m.Identity.id.in_(archived_by),
        m.StaffWish: wish_scope,
        m.WishOverride: m.WishOverride.wish_id.in_(select(m.StaffWish.id).where(wish_scope)),
        m.WishLedger: m.WishLedger.department_id.in_(preview_departments)
        & m.WishLedger.metric.in_(('wishes_granted', 'wishes_declined')),
        m.FairnessLedger: m.FairnessLedger.department_id.in_(preview_departments),
        m.PublicationChangeList: department(m.PublicationChangeList),
        history: department(history) & history_scope,
        m.Job: m.Job.workspace_id.in_(workspace_ids) & (m.Job.kind == 'generation') & (m.Job.status == 'applied'),
    })
    # A baseline may reference a local session's definition even if it is absent from the shared catalogue.
    for cls, references in ((m.AssignmentType, select(m.Shift.assignment_type_id).where(shift_scope)),
                            (m.Location, select(m.Shift.location_id).where(shift_scope)),
                            (m.Service, select(m.ShiftService.service_id).where(m.ShiftService.shift_id.in_(shifts)))):
        sources[cls] = or_(sources[cls], cls.id.in_(references))
    projected = {m.Identity: ('id', 'person_id', 'disabled', 'display_name'),
                 m.Job: ('id', 'workspace_id', 'kind', 'status', 'read_metadata'),
                 m.AnnouncementReceipt: ('id', 'event_occurrence_id', 'person_id', 'response')}
    from .services.wishes import SETTLED
    candidate = m.Workspace.id.in_(workspace_ids)
    unissued = m.Workspace.id.not_in(select(m.Publication.workspace_id))
    reservations = m.Workspace.department_id.in_(preview_departments) & m.Workspace.status.in_(SETTLED) & unissued
    workspace_columns = {key: case((candidate, m.Workspace.__table__.c[key]))
                         for key in ('review_snapshot', 'archive_state', 'target_overrides', 'rule_adjustments',
                                     'accepted_issues', 'return_note')}
    workspace_columns['validation'] = case((candidate | reservations, m.Workspace.validation))
    # Read candidate baseline heads alongside the workspace rows in the trial.
    workspace_table = m.Workspace.__table__
    baseline_table = m.WorkspaceBaseline.__table__
    ranked = ranked_baseline_keys(select(baseline_table.c.id)
        .select_from(workspace_table.join(baseline_table, workspace_table.c.id == baseline_table.c.workspace_id))
        .where(candidate))
    source = workspace_table.outerjoin(ranked, (ranked.c.workspace_id == workspace_table.c.id) & (ranked.c.position == 1))
    workspace_keys, workspaces_with_baselines = _raw_rows(db, m.Workspace, sources.pop(m.Workspace),
        projected=workspace_columns, source=source, trailing=(ranked.c.id, ranked.c.payload_hash))
    rows = {cls.__tablename__: _raw_rows(db, cls, predicate, projected.get(cls)) for cls, predicate in sources.items()}
    rows[workspace_table.name] = workspace_keys, [row[:-2] for row in workspaces_with_baselines]
    baselines = []
    for row in workspaces_with_baselines:
        ident, payload_hash = row[-2:]
        if ident is not None:
            key = ident, payload_hash
            # Only unchanged immutable rows may reuse the payload verified by the before reader.
            baselines.append(before_baselines[key] if key in before_baselines else
                             _baseline_source_row(db.get(m.WorkspaceBaseline, ident)))
    rows[m.Person.__tablename__] = person_keys, person_rows
    rows[m.Availability.__tablename__] = _raw_rows(db, m.Availability,
        m.Availability.person_id.in_(people) & m.Availability.retracted.is_(False),
        projected={'private_reason': literal(None).label('private_reason')})
    ordered_sources = (m.Eligibility, m.SchedulingFact, m.StaffWish)
    seq_scope = or_(*((m.RecordInsertion.table_name == cls.__tablename__)
                      & m.RecordInsertion.record_id.in_(select(cls.id).where(sources[cls])) for cls in ordered_sources))
    rows[m.RecordInsertion.__tablename__] = _raw_rows(db, m.RecordInsertion, seq_scope)
    return pickle.dumps((captured_at, rows), protocol=pickle.HIGHEST_PROTOCOL), baselines, issued_sources


@contextmanager
def catalogue_source_reader(engine, captured, actor):
    """Run the normal builder against only captured rows with the source dialect's SQL semantics."""
    postgres = engine.dialect.name == 'postgresql'
    replay = engine if postgres else create_engine('sqlite://', hide_parameters=True).execution_options(
        **engine.get_execution_options())
    try:
        with replay.connect() as connection:
            connection.execution_options(compiled_cache={})
            if postgres:
                connection.exec_driver_sql('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                connection.exec_driver_sql('SET LOCAL search_path TO pg_temp, pg_catalog')
            encoded, baselines, issued_sources = captured
            captured_at, sources = pickle.loads(encoded)
            for table, (keys, rows) in issued_sources.items():
                if table in sources:
                    sources[table][1].extend(rows)
                else:
                    sources[table] = keys, rows
            baseline_table = m.WorkspaceBaseline.__table__
            sources[baseline_table.name] = baseline_table.c.keys(), baselines
            metadata = MetaData()
            for name, (keys, rows) in sources.items():
                original = m.Base.metadata.tables[name]
                table = Table(name, metadata, *(Column(key, original.c[key].type, primary_key=original.c[key].primary_key)
                                                 for key in keys), prefixes=['TEMPORARY'])
                table.create(connection)
                if rows:
                    # Preserve the distinction between SQL NULL and a stored JSON null.
                    json_values = {key: cast(bindparam(key, type_=Text), original.c[key].type) if postgres
                                   else bindparam(key, type_=Text) for key in keys
                                   if original is not baseline_table and isinstance(original.c[key].type, JSON)}
                    connection.execute(table.insert().values(json_values), [dict(zip(keys, row)) for row in rows])
            if postgres:
                connection.exec_driver_sql('SET TRANSACTION READ ONLY')
            else:
                connection.exec_driver_sql('PRAGMA query_only = ON')
            with Session(connection, autoflush=False) as db:
                db.info.update(workspace_actor=actor, _request_parents={}, _request_workspace_criteria={})
                yield db, captured_at
    finally:
        if not postgres:
            replay.dispose()
