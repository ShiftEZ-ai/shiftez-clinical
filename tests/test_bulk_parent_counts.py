"""Parent counts stay bounded when the related-record menu fans out."""

from sqlalchemy import event

from apps.api.db import transaction
from apps.api.services import bulk_selection as bulk
from packages.contracts import schemas as s
from tests.test_bulk_selection import actor


def test_one_person_parent_counts_use_bounded_reads(h):
    person = h.seed["people"][0]
    parent = {"collection": "people", "selection": {"collection": "people", "include_ids": [person]}}
    children = tuple(s.BULK_COLLECTIONS["people"]["children"])
    selects = []

    def sql(_connection, _cursor, statement, *_):
        if statement.lstrip().upper().startswith("SELECT"):
            selects.append(statement)

    with transaction(h.app.state.engine) as db:
        current = actor(db, h)
        event.listen(h.app.state.engine, "before_cursor_execute", sql)
        try:
            for child in children:
                result = bulk.resolve(db, current, {"collection": child, "parent": parent})
                assert result["count"] == sum(result["by_parent"].values())
        finally:
            event.remove(h.app.state.engine, "before_cursor_execute", sql)

    # This is a query/work bound, not a wall-clock budget.  Each child gets a
    # bounded parent read, relation read and optional sample projection.
    assert len(selects) <= len(children) * 12
