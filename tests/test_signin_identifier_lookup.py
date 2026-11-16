"""Derived alias indexes preserve Unicode matching without scanning account records."""
from datetime import datetime, timezone

import pytest
from alembic import command
from sqlalchemy import MetaData, Table, insert, select, text

from apps.api import auth, models as m
from apps.api.db import make_engine, transaction
from apps.api.serialization import record
from apps.api.services.common import materialise_defaults
from scripts import manage
from .test_bulk_pending_privacy import anonymise


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
def test_pending_identifier_defaults_follow_final_insert_values(request, database):
    h = request.getfixturevalue(database)
    with transaction(h.app.state.engine, write=True) as db:
        identity = m.Identity(issuer="local-development", subject="Original.Subject", display_name="Synthetic (test)")
        materialise_defaults(identity)
        identity.subject = "Final.Subject"
        db.add(identity)
        db.flush()
        assert identity.subject_normalized == "final.subject"
        account = m.Account(identity_id=identity.id, username="Original.User", email="Original@example.test")
        materialise_defaults(account)
        account.username, account.email = "Final.User", "Final@example.test"
        db.add(account)
        db.flush()
        assert account.username_normalized == "final.user" and account.email_normalized == "final@example.test"
        for identifier in ("Original.Subject", "Original.User", "Original@example.test"):
            assert auth.resolve_identifier(db, identifier, h.settings) == (None, None)


@pytest.mark.parametrize("database", ["h", pytest.param("pg", marks=pytest.mark.postgres)])
@pytest.mark.parametrize("has_account", [False, True])
def test_anonymised_identifiers_no_longer_resolve(request, database, has_account):
    h = request.getfixturevalue(database)
    person = h.configure("people", {"display_name": "Synthetic sign-in owner", "active": False})["record"]
    with transaction(h.app.state.engine, write=True) as db:
        identity = m.Identity(issuer="local-development", subject="Private.Subject", display_name="Private (test)",
                              person_id=person["id"])
        db.add(identity)
        db.flush()
        identifiers = [identity.subject]
        if has_account:
            account = m.Account(identity_id=identity.id, username="Private.User", email="Private@example.test")
            db.add(account)
            db.flush()
            identifiers.extend([account.username, account.email])
        for identifier in identifiers:
            assert auth.resolve_identifier(db, identifier, h.settings)[1].id == identity.id
        with db.no_autoflush:
            db.info["bulk"] = {"inputs_changed": False, "availability_changed": False, "history": []}
            anonymise(db, h, db.get(m.Person, person["id"]))
    with transaction(h.app.state.engine) as db:
        for identifier in identifiers:
            assert auth.resolve_identifier(db, identifier, h.settings) == (None, None)


def test_deferred_defaults_receive_current_identifier_and_job_values():
    identity = m.Identity(issuer="synthetic", subject="Straße", display_name="Synthetic (test)")
    account = m.Account(identity_id="synthetic", username="Straße", email="Straße@example.test")
    job = m.Job(kind="validate", input_snapshot={"people": []}, result=None)
    for row in (identity, account, job):
        materialise_defaults(row)
        assert row.id and row.revision == 1
    assert identity.subject_normalized == account.username_normalized == "strasse"
    assert account.email_normalized == "strasse@example.test"
    assert job.read_metadata is not None


def test_identifier_lookup_columns_follow_orm_changes_and_stay_out_of_replies(h):
    identity_id = h.seed["identities"]["staff"]
    with transaction(h.app.state.engine, write=True) as db:
        identity = db.get(m.Identity, identity_id)
        identity.subject = "Straße"
        account = m.Account(identity_id=identity_id, username="initial.user", email="Straße@example.test")
        db.add(account)
        db.flush()
        assert auth.resolve_identifier(db, "STRASSE", h.settings)[1].id == identity_id
        assert auth.resolve_identifier(db, "STRASSE@EXAMPLE.TEST", h.settings)[1].id == identity_id
        assert "subject_normalized" not in record(identity)
        assert not {"username_normalized", "email_normalized"}.intersection(record(account))
        account.username, account.email = "renamed.user", "Changed@example.test"
        identity.subject = "Öther"
        db.flush()
        for previous in ("initial.user", "strasse@example.test", "strasse"):
            assert auth.resolve_identifier(db, previous, h.settings) == (None, None)
        for current in ("RENAMED.USER", "CHANGED@EXAMPLE.TEST", "ÖTHER"):
            assert auth.resolve_identifier(db, current, h.settings)[1].id == identity_id


def test_core_insert_defaults_support_indexed_department_scale_lookup(h):
    with transaction(h.app.state.engine, write=True) as db:
        identities = [{"id": m.uid(), "issuer": "local-development", "subject": f"person-{index}",
                       "display_name": f"Person {index} (test)"} for index in range(150)]
        db.execute(insert(m.Identity), identities)
        db.execute(insert(m.Account), [{"identity_id": identity["id"], "username": f"user-{index}",
                                       "email": f"Person-{index}@example.test"}
                                      for index, identity in enumerate(identities)])
        assert auth.resolve_identifier(db, "PERSON-149@EXAMPLE.TEST", h.settings)[1].id == identities[-1]["id"]
        assert auth.resolve_identifier(db, "PERSON-149", h.settings)[1].id == identities[-1]["id"]
        plan = " ".join(row[3] for row in db.execute(text(
            "EXPLAIN QUERY PLAN SELECT id FROM accounts WHERE username_normalized=:identifier "
            "OR email_normalized=:identifier LIMIT 2"), {"identifier": "person-149@example.test"}))
        assert "ix_accounts_username_normalized" in plan and "ix_accounts_email_normalized" in plan
        assert "SCAN accounts" not in plan


def test_identifier_migration_fills_only_new_columns_and_replays_without_changes(tmp_path):
    url = "sqlite:///" + str(tmp_path / "identifier-upgrade.db")
    config = manage.alembic_config(url)
    command.upgrade(config, "0120_auth_configuration")
    engine = make_engine(url)
    try:
        metadata = MetaData()
        identities, accounts = [Table(name, metadata, autoload_with=engine) for name in ("identities", "accounts")]
        with engine.begin() as connection:
            fields = {"revision": 7, "created_at": datetime(2026, 10, 3, tzinfo=timezone.utc)}
            connection.execute(identities.insert().values(**fields, id="retained-identity", issuer="local-development",
                               subject="Straße", display_name="Retained person (test)", disabled=False))
            connection.execute(accounts.insert().values(**fields, id="retained-account", identity_id="retained-identity",
                               username="retained.user", email="Straße@example.test"))
            before = [connection.execute(select(table)).all() for table in (identities, accounts)]
        command.upgrade(config, "head")
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert [connection.execute(select(table)).all() for table in (identities, accounts)] == before
            assert connection.execute(text("SELECT subject_normalized FROM identities")).scalar_one() == "strasse"
            assert connection.execute(text("SELECT username_normalized, email_normalized FROM accounts")).one() == (
                "retained.user", "strasse@example.test")
    finally:
        engine.dispose()
