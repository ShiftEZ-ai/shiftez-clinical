"""The acquired table set must be fenced by the current writer stamp."""
import re
from sqlalchemy import event, Table
from sqlalchemy.sql import visitors
from apps.api import models as m, snapshots
from apps.api.db import snapshot_input_listener_classes, transaction
from tests.test_fix_list_performance import backend


def test_every_acquired_table_is_revision_tracked_or_workspace_owned(backend, request):
    h = backend
    workspace = h.shift(h.workspace())["workspace"]
    tables = set()
    def read(_connection, _cursor, statement, _parameters, context, _many):
        if statement.lstrip().upper().startswith(("SELECT", "WITH")):
            if context.compiled is not None:
                tables.update(node.name for node in visitors.iterate(context.compiled.statement)
                              if isinstance(node, Table))
            tables.update(re.findall(r'\b(?:FROM|JOIN)\s+["`]?([a-z_][a-z_0-9]*)', statement, re.I))
    with transaction(h.app.state.engine) as db:
        row = db.get(m.Workspace, workspace["id"])
        event.listen(h.app.state.engine, "before_cursor_execute", read)
        try:
            snapshots._domain_snapshot(db, row)
            snapshots._domain_snapshot(db, row, current_scheduling=True, mandatory_rules=True)
        finally:
            event.remove(h.app.state.engine, "before_cursor_execute", read)
        stamp = snapshots.currency_hint(db, row)
        org = db.get(m.Organisation, h.seed["organisation_id"])
        assert (stamp["input_revision"], stamp["availability_revision"], stamp["content_revision"]) == (
            org.input_revision, org.availability_revision, row.content_revision)
    listeners = snapshot_input_listener_classes()
    tracked = {cls.__table__.name for cls in listeners}
    # Insertion ordinals are immutable trigger output for these revision-tracked source rows.
    tracked.update(node.name for cls in listeners if issubclass(cls, m.InsertionOrdered)
                   for node in visitors.iterate(cls.seq.__clause_element__()) if isinstance(node, Table))
    assert {cls for _, cls, *_ in snapshots.snapshot_input_specs()} == set(listeners)
    assert set(listeners.values()) == {"general", "availability"}
    owned = {m.Workspace.__table__}
    while linked := {table for table in m.Base.metadata.tables.values() if table not in owned
                    and any(key.column.table in owned for key in table.foreign_keys)}:
        owned.update(linked)
    workspace_tables = {table.name for table in owned}
    assert tables, "No acquisition SQL was captured"
    request.node.user_properties.append(("acquired_tables", ",".join(sorted(tables))))
    assert not tables - tracked - workspace_tables, sorted(tables - tracked - workspace_tables)


def test_planning_group_version_write_moves_input_revision(backend):
    from tests.test_planning_groups_drafts import group, settings
    h = backend
    settings(h)
    workspace = h.workspace()
    with transaction(h.app.state.engine) as db:
        before = snapshots.currency_hint(db, db.get(m.Workspace, workspace["id"]))
    group(h)
    with transaction(h.app.state.engine) as db:
        after = snapshots.currency_hint(db, db.get(m.Workspace, workspace["id"]))
    assert after["input_revision"] == before["input_revision"] + 1
    assert after["availability_revision"] == before["availability_revision"]
