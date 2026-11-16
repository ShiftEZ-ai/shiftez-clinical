"""The score and scheduling snapshot share one read of department configuration."""
from sqlalchemy import event

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import planning


def test_revalidation_reads_department_configuration_once(h):
    workspace = h.workspace()
    reads = []

    def count(_connection, _cursor, statement, _parameters, _context, _many):
        sql = " ".join(statement.split())
        if sql.startswith("SELECT") and "FROM departments WHERE departments.id =" in sql:
            reads.append(sql)

    with transaction(h.app.state.engine, write=True) as db:
        row = db.get(m.Workspace, workspace["id"])
        event.listen(h.app.state.engine, "before_cursor_execute", count)
        try:
            checked = planning.revalidate(db, row)
        finally:
            event.remove(h.app.state.engine, "before_cursor_execute", count)
        assert "score" in checked and checked.get("scope") != "swap"
        assert '_session_parts' not in checked
        assert row.validation == {**checked, '_session_parts': [], '_person_parts': {}}
    assert len(reads) == 1
