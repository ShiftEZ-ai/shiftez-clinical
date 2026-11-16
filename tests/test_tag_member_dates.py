"""Slice 0 schema checks for optional person tag membership dates (the contract lands with slice 1).

Both fixtures build their schema by running the migrations, so each case proves the migrated
check on the SQLite and the PostgreSQL path, including the cross-column kind clause.
"""
from datetime import date
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from apps.api import models as m
from apps.api.db import transaction

D1, D8 = date(2026, 10, 1), date(2026, 10, 8)


@pytest.fixture(params=["h", pytest.param("pg", marks=pytest.mark.postgres)], ids=["sqlite", "postgres"])
def both(request):
    return request.getfixturevalue(request.param)


def tag_id(app):
    with transaction(app.app.state.engine, write=True) as db:
        tag = m.Tag(department_id=app.seed["department_id"], name=f"Dated {uuid.uuid4().hex[:12]}", labels=["people"])
        db.add(tag)
        db.flush()
        return tag.id


def insert(app, tag, kind="people", starts_on=None, ends_on=None):
    with app.app.state.engine.begin() as db:  # raw insert: the database check, not the ORM, must decide
        db.execute(m.TagMember.__table__.insert().values(
            id=str(uuid.uuid4()), revision=1, created_at=m.now(), department_id=app.seed["department_id"],
            tag_id=tag, kind=kind, member_id=str(uuid.uuid4()), starts_on=starts_on, ends_on=ends_on))


def test_existing_memberships_stay_open_ended(both):
    with both.app.state.engine.connect() as db:
        assert all(row == (None, None) for row in db.execute(text("SELECT starts_on, ends_on FROM tag_members")))


@pytest.mark.parametrize("starts_on, ends_on", [(D1, D8), (None, D8), (D1, None), (None, None)],
                         ids=["closed", "open-start", "open-end", "undated"])
def test_person_member_dates_are_accepted(both, starts_on, ends_on):
    insert(both, tag_id(both), starts_on=starts_on, ends_on=ends_on)


@pytest.mark.parametrize("kind, starts_on, ends_on", [
    ("people", D8, D1),          # reversed range
    ("people", D1, D1),          # the end is exclusive: an empty range is refused
    ("services", D1, None),      # only person members may carry dates (kind clause)
    ("locations", None, D8),
], ids=["reversed", "empty", "service-start", "location-end"])
def test_invalid_member_dates_are_refused_by_the_database(both, kind, starts_on, ends_on):
    with pytest.raises(IntegrityError):
        insert(both, tag_id(both), kind=kind, starts_on=starts_on, ends_on=ends_on)


def test_undated_non_person_members_are_unchanged(both):
    insert(both, tag_id(both), kind="services")
