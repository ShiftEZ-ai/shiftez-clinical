"""Freeze the relational reads needed by a catalogue trial before it rolls back."""
import pickle
from contextlib import contextmanager

from sqlalchemy import JSON, Column, MetaData, Table, Text, bindparam, case, cast, create_engine, literal, or_, select
from sqlalchemy.orm import Session

from . import models as m
from .snapshots import snapshot_department_ids, snapshot_input_specs
from .workspace_baselines import latest_many


def _raw_rows(db, cls, predicate, keys=None, *, projected=None):
    table = cls.__table__
    keys = table.c.keys() if keys is None else keys
    projected = projected or {}
    columns = [projected.get(key, table.c[key]) for key in keys]
    columns = [cast(column, Text).label(key) if isinstance(table.c[key].type, JSON) else column
               for key, column in zip(keys, columns)]
    return keys, [tuple(row) for row in db.execute(select(*columns).where(predicate))]


def capture_catalogue_sources(db, organisation_id, workspaces, *, captured_at):
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
    publication_scope = or_(m.Publication.department_id.in_(department_ids) & m.Publication.current.is_(True),
                            m.Publication.workspace_id.in_(workspace_ids),
                            m.Publication.id.in_([w.base_publication_id for w in workspaces if w.base_publication_id]))
    external = select(m.Publication.workspace_id).where(publication_scope)
    shift_scope = m.Shift.workspace_id.in_(workspace_ids) | m.Shift.workspace_id.in_(external)
    shifts = select(m.Shift.id).where(shift_scope)
    rotations = select(m.Rotation.id).where(m.Rotation.department_id.in_(department_ids))
    occurrences = select(m.EventOccurrence.id).where(m.EventOccurrence.department_id.in_(department_ids))
    baselines = latest_many(db, workspace_ids)

    def department(cls):
        return cls.department_id.in_(department_ids)

    wish_scope = m.StaffWish.department_id.in_(preview_departments) & (m.StaffWish.status == 'open')

    history = m.DefinitionHistory
    history_kinds = ('scheduling_fact',)
    if any(w.status == 'published' for w in workspaces):
        history_kinds += ('date_sets', 'exception', 'policy', 'catalogue_conversion')
        history_scope = history.kind.in_(history_kinds)
    else:
        adjusted = {row['policy_id'] for w in workspaces for row in w.rule_adjustments or ()}
        history_scope = or_(history.kind.in_(history_kinds), (history.kind == 'policy') & history.object_id.in_(adjusted))

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
        m.WorkspaceBaseline: m.WorkspaceBaseline.id.in_([row.id for row in baselines.values()]),
        m.PlanningGroupVersion: department(m.PlanningGroupVersion),
        m.Shift: shift_scope,
        m.ShiftService: m.ShiftService.shift_id.in_(shifts),
        m.Assignment: m.Assignment.shift_id.in_(shifts),
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
    rows = {cls.__tablename__: _raw_rows(db, cls, predicate, projected.get(cls)) for cls, predicate in sources.items()}
    rows[m.Person.__tablename__] = person_keys, person_rows
    rows[m.Availability.__tablename__] = _raw_rows(db, m.Availability,
        m.Availability.person_id.in_(people) & m.Availability.retracted.is_(False),
        projected={'private_reason': literal(None).label('private_reason')})
    # Wish queries distinguish a superseded publication from a settled draft without any publication.
    # Keep the join metadata for both, and load issued payloads only where the builder may open them.
    rows[m.Publication.__tablename__] = _raw_rows(db, m.Publication,
        m.Publication.department_id.in_(preview_departments) | publication_scope,
        projected={key: case((publication_scope, m.Publication.__table__.c[key])) for key in ('snapshot', 'validation')})
    ordered_sources = (m.Eligibility, m.SchedulingFact, m.StaffWish)
    seq_scope = or_(*((m.RecordInsertion.table_name == cls.__tablename__)
                      & m.RecordInsertion.record_id.in_(select(cls.id).where(sources[cls])) for cls in ordered_sources))
    rows[m.RecordInsertion.__tablename__] = _raw_rows(db, m.RecordInsertion, seq_scope)
    return pickle.dumps((captured_at, rows), protocol=pickle.HIGHEST_PROTOCOL)


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
            captured_at, sources = pickle.loads(captured)
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
                                   if isinstance(original.c[key].type, JSON)}
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
