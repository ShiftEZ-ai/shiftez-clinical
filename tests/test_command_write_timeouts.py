"""Contention aborts whole writes within a bound, with a safe error and usable reads."""
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import logging
import sqlite3
from threading import Event
import time
from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

from apps.api import db
from apps.api.main import create_app
from apps.api.settings import Settings
from .conftest import TEST_PASSWORD


@pytest.fixture
def bounded(pg,monkeypatch):
    settings=replace(pg.settings,command_lock_timeout_ms=80,command_statement_timeout_ms=150,
        command_write_budget_ms=500,db_pool_timeout_seconds=1)
    engine=db.make_engine(settings.database_url,settings=settings)
    monkeypatch.setattr(db,"WRITE_LOCK_KEY",1_000_000_000+uuid.uuid4().int%1_000_000_000)
    # The diagnostic rate limit is per process; an earlier test's diagnostic must not suppress this one.
    monkeypatch.setattr(db,"_lock_diagnostic_last_at",None)
    monkeypatch.setattr(db,"_lock_diagnostic_suppressed",0)
    yield settings,engine
    engine.dispose()


@pytest.fixture
def advisory_lock_timeout(bounded,pg,caplog):
    settings,engine=bounded
    client=TestClient(create_app(settings,engine),base_url=settings.public_origin)
    try:
        client.headers["origin"]=settings.public_origin
        with TestClient(pg.app,base_url=settings.public_origin) as login_client:
            login_client.headers["origin"]=settings.public_origin
            login=login_client.post("/auth/login",json={"username":"coordinator","password":TEST_PASSWORD})
            assert login.status_code==200,login.text
            client.cookies.update(login_client.cookies)
        client.headers["x-csrf-token"]=client.cookies["rota_csrf"]
        key=str(uuid.uuid4())
        with engine.begin() as holder:
            holder.execute(text("SELECT pg_advisory_xact_lock(:key)"),{"key":db.WRITE_LOCK_KEY})
            holder_pid=holder.scalar(text("SELECT pg_backend_pid()"))
            before=client.get("/api/me")
            started=time.monotonic()
            with caplog.at_level(logging.INFO):
                response=client.post("/api/commands",headers={"idempotency-key":key},json={"kind":"workspace.create",
                    "payload":{"name":"Private synthetic lock fixture","starts_on":"2026-10-04","ends_on":"2026-10-11"}})
            elapsed=time.monotonic()-started
            after=client.get("/api/me")
        with db.transaction(engine) as session:
            receipts=session.scalar(text("SELECT count(*) FROM command_receipts WHERE key=:key"),{"key":key})
            workspaces=session.scalar(text("SELECT count(*) FROM workspaces WHERE name=:name"),{"name":"Private synthetic lock fixture"})
        records=json.dumps([vars(record) for record in caplog.records],default=str)
        return login,before,response,after,receipts,workspaces,records,holder_pid,elapsed
    finally:
        client.close()


@pytest.mark.postgres
def test_command_advisory_lock_times_out_without_receipt_and_reads_stay_usable(advisory_lock_timeout):
    login,before,response,after,receipts,workspaces,records,holder_pid,_=advisory_lock_timeout
    assert login.status_code==200
    assert before.status_code==200
    assert response.status_code==503,response.text
    assert response.json()["error"]["code"]=="COMMAND_WRITE_TIMEOUT"
    assert response.json()["error"]["message"]=="The system is busy, so this change was not saved. Try again."
    assert after.status_code==200
    assert receipts==0
    assert workspaces==0
    assert str(holder_pid) in records
    assert "Private synthetic lock fixture" not in records and TEST_PASSWORD not in records


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_command_advisory_lock_timeout_cpu_budget(advisory_lock_timeout):
    assert advisory_lock_timeout[-1]<2


@pytest.fixture(scope="module",params=[pytest.param("postgresql",marks=pytest.mark.postgres),"sqlite"])
def serialized_writer_wait(scale_harness,request,tmp_path_factory):
    backend=request.param
    tmp_path=tmp_path_factory.mktemp("serialized-writer")
    monkeypatch=pytest.MonkeyPatch()
    settings=scale_harness("pg").settings if backend=="postgresql" else Settings(
        environment="test",database_url="sqlite:///"+str(tmp_path/"queued.db"))
    settings=replace(settings,command_lock_timeout_ms=80,command_statement_timeout_ms=200,command_write_budget_ms=10_000)
    engine=db.make_engine(settings.database_url,settings=settings)
    monkeypatch.setattr(db,"WRITE_LOCK_KEY",1_000_000_000+uuid.uuid4().int%1_000_000_000)
    held,release=Event(),Event()
    operations=[]
    @event.listens_for(engine,"commit")
    def record_commit(connection):
        if connection.info.get("rota_write"):
            operations.append("commit")
    clock=[0.0]
    # Advance only the database deadline clock; the real lock hand-off stays event-driven.
    monkeypatch.setattr(db,"time",SimpleNamespace(monotonic=lambda:clock[0],sleep=time.sleep))
    serialization_limits=[]
    @event.listens_for(engine,"before_cursor_execute")
    def release_at_queued_lock(connection,cursor,statement,parameters,context,many):
        if held.is_set() and (statement=="BEGIN IMMEDIATE" or statement.startswith("SELECT pg_advisory_xact_lock")):
            state=connection.info["rota_write"]
            assert state["serializing"]
            if backend=="postgresql":
                cursor.execute("SELECT setting::integer FROM pg_settings WHERE name IN ('lock_timeout','statement_timeout') ORDER BY name")
                limits=tuple(row[0] for row in cursor.fetchall())
            else:
                cursor.execute("PRAGMA busy_timeout")
                limits=(cursor.fetchone()[0],)
                cursor.execute("PRAGMA busy_timeout=0")
                with pytest.raises(sqlite3.OperationalError,match="database is locked"):
                    cursor.execute("BEGIN IMMEDIATE")
                cursor.execute(f"PRAGMA busy_timeout={limits[0]}")
            serialization_limits.append(limits)
            assert limits==(settings.command_write_budget_ms,)*(2 if backend=="postgresql" else 1)
            clock[0]=5.0
            release.set()
    def first_writer():
        with db.transaction(engine,write=True) as session:
            operations.append("first operation")
            session.execute(text("INSERT INTO command_timeout_probe VALUES (1)"))
            held.set()
            assert release.wait(10)
    def queued_writer(session):
        operations.append("second operation")
        session.execute(text("INSERT INTO command_timeout_probe VALUES (2)"))
        state=session.connection().info["rota_write"]
        assert not state["serializing"]
        assert state["limits"]==(settings.command_lock_timeout_ms,settings.command_statement_timeout_ms)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE command_timeout_probe (id integer PRIMARY KEY)"))
        with ThreadPoolExecutor(max_workers=1) as executor:
            first=executor.submit(first_writer)
            try:
                assert held.wait(10)
                db.retry_write(engine,queued_writer)
            finally:
                release.set()
                first.result(timeout=10)  # A holder failure must remain visible when the contender also fails.
        with db.transaction(engine) as session:
            rows=session.scalars(text("SELECT id FROM command_timeout_probe ORDER BY id")).all()
        return rows,operations,clock[0],serialization_limits
    finally:
        engine.dispose()
        monkeypatch.undo()


def test_serialized_writer_can_wait_five_seconds_past_lock_and_statement_limits(serialized_writer_wait):
    rows,operations,deadline,serialization_limits=serialized_writer_wait
    assert rows==[1,2]
    assert operations==["first operation","commit","second operation","commit"]
    assert deadline==5.0 and len(serialization_limits)==1


@pytest.fixture
def row_lock_timeout(bounded):
    _,engine=bounded
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE command_timeout_probe (id integer PRIMARY KEY, value integer)"))
        connection.execute(text("INSERT INTO command_timeout_probe VALUES (1,1)"))
    with engine.begin() as holder:
        holder.execute(text("UPDATE command_timeout_probe SET value=2 WHERE id=1"))
        def operation(session):
            session.execute(text("INSERT INTO command_timeout_probe VALUES (2,99)"))
            session.execute(text("UPDATE command_timeout_probe SET value=3 WHERE id=1"))
        started=time.monotonic()
        with pytest.raises(db.DatabaseWriteTimeout):
            db.retry_write(engine,operation)
        elapsed=time.monotonic()-started
        with db.transaction(engine) as session:
            during=session.execute(text("SELECT id,value FROM command_timeout_probe ORDER BY id")).all()
    with db.transaction(engine) as session:
        after=session.execute(text("SELECT id,value FROM command_timeout_probe ORDER BY id")).all()
    return during,after,elapsed


@pytest.mark.postgres
def test_row_lock_timeout_rolls_back_prior_writes(row_lock_timeout):
    during,after,_=row_lock_timeout
    assert during==[(1,1)]
    assert after==[(1,2)]


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_row_lock_timeout_cpu_budget(row_lock_timeout):
    assert row_lock_timeout[-1]<2


@pytest.fixture
def statement_timeout(bounded):
    _,engine=bounded
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE command_timeout_probe (id integer PRIMARY KEY)"))
        before=connection.scalar(text("SHOW statement_timeout"))
    def operation(session):
        session.execute(text("INSERT INTO command_timeout_probe VALUES (1)"))
        session.execute(text("SELECT pg_sleep(1)"))
    started=time.monotonic()
    with pytest.raises(db.DatabaseWriteTimeout):
        db.retry_write(engine,operation)
    elapsed=time.monotonic()-started
    with db.transaction(engine) as session:
        count=session.scalar(text("SELECT count(*) FROM command_timeout_probe"))
        after=session.scalar(text("SHOW statement_timeout"))
    return count,before,after,elapsed


@pytest.mark.postgres
def test_statement_timeout_rolls_back_and_does_not_leak_into_reads(statement_timeout):
    count,before,after,_=statement_timeout
    assert count==0
    assert after==before


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_statement_timeout_cpu_budget(statement_timeout):
    *_,elapsed=statement_timeout
    assert elapsed<2


@pytest.fixture
def exhausted_pool_timeout(bounded):
    settings,_=bounded
    engine=create_engine(settings.database_url,pool_size=1,max_overflow=0,pool_timeout=.08,
        hide_parameters=True).execution_options(rota_write_settings=settings)
    try:
        with engine.connect():
            started=time.monotonic()
            with pytest.raises(db.DatabaseWriteTimeout) as failure:
                db.retry_write(engine,lambda session:session.execute(text("SELECT 1")))
            elapsed=time.monotonic()-started
            return failure.value,elapsed
    finally:
        engine.dispose()


@pytest.mark.postgres
def test_exhausted_pool_fails_with_bounded_command_error(exhausted_pool_timeout):
    failure,_=exhausted_pool_timeout
    assert failure.status==503 and failure.code=="COMMAND_WRITE_TIMEOUT"


@pytest.mark.postgres
@pytest.mark.wall_benchmark
def test_exhausted_pool_timeout_cpu_budget(exhausted_pool_timeout):
    assert exhausted_pool_timeout[-1]<2


@pytest.mark.postgres
def test_blocker_diagnostic_query_failure_preserves_outer_transaction(bounded,caplog):
    _,engine=bounded
    @event.listens_for(engine,"before_cursor_execute",retval=True)
    def reject_diagnostic(connection,cursor,statement,parameters,context,executemany):
        return ("SELECT 1/0",{}) if "FROM pg_locks" in statement else (statement,parameters)
    with db.transaction(engine) as session:
        assert db._advisory_blockers(session)==[]
        assert session.scalar(text("SELECT 1"))==1
    assert any(record.message=="write_lock_diagnostics_unavailable" for record in caplog.records)


@pytest.fixture(scope="module")
def sqlite_held_writer_timeout(tmp_path_factory):
    tmp_path=tmp_path_factory.mktemp("held-writer")
    settings=Settings(environment="test",database_url="sqlite:///"+str(tmp_path/"locked.db"),
        command_lock_timeout_ms=50,command_statement_timeout_ms=100,command_write_budget_ms=120)
    engine=db.make_engine(settings.database_url,settings=settings)
    calls=[]
    try:
        with engine.connect() as holder:
            holder.execute(text("BEGIN IMMEDIATE"))
            started=time.monotonic()
            with pytest.raises(db.DatabaseWriteTimeout):
                db.retry_write(engine,lambda session:calls.append(session),attempts=100)
            elapsed=time.monotonic()-started
        return calls,elapsed
    finally:
        engine.dispose()


def test_sqlite_held_writer_times_out_without_running_operation(sqlite_held_writer_timeout):
    calls,_=sqlite_held_writer_timeout
    assert not calls


@pytest.mark.wall_benchmark
def test_sqlite_held_writer_timeout_cpu_budget(sqlite_held_writer_timeout):
    assert sqlite_held_writer_timeout[-1]<1


def test_retry_budget_stops_transient_failures_without_real_delays(tmp_path,monkeypatch):
    settings=Settings(environment="test",database_url="sqlite:///"+str(tmp_path/"retry.db"),command_write_budget_ms=50)
    engine=db.make_engine(settings.database_url,settings=settings)
    clock=[0.0]
    calls=[]
    @contextmanager
    def transaction(*args,**kwargs):
        yield None
    def operation(_session):
        calls.append(clock[0])
        raise OperationalError("UPDATE synthetic_probe",{},Exception("database is locked"))
    monkeypatch.setattr(db,"transaction",transaction)
    monkeypatch.setattr(db.time,"monotonic",lambda:clock[0])
    monkeypatch.setattr(db.time,"sleep",lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    try:
        with pytest.raises(db.DatabaseWriteTimeout):
            db.retry_write(engine,operation,attempts=100)
        assert 1<len(calls)<100 and .05<=clock[0]<.1
    finally:
        engine.dispose()


@pytest.mark.parametrize('detail', [
    'connection failed: FATAL: the database system is in recovery mode',
    'connection to server at "db" port 5432 failed: the database system is not yet accepting connections',
])
def test_retry_budget_converts_postgres_recovery_connections_to_bounded_timeout(tmp_path,monkeypatch,detail):
    settings=Settings(environment='test',database_url='sqlite:///'+str(tmp_path/'recovery.db'),command_write_budget_ms=50)
    engine=db.make_engine(settings.database_url,settings=settings)
    clock=[0.0]
    calls=[]
    @contextmanager
    def transaction(*args,**kwargs):
        yield None
    def operation(_session):
        calls.append(clock[0])
        raise OperationalError('SELECT 1', {}, Exception(detail))
    monkeypatch.setattr(db,'transaction',transaction)
    monkeypatch.setattr(db.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(db.time,'sleep',lambda seconds:clock.__setitem__(0,clock[0]+seconds))
    try:
        with pytest.raises(db.DatabaseWriteTimeout):
            db.retry_write(engine,operation,attempts=100)
        assert 1 < len(calls) < 100 and .05 <= clock[0] < .1
    finally:
        engine.dispose()


@pytest.fixture(scope="module")
def sqlite_statement_timeout(tmp_path_factory):
    tmp_path=tmp_path_factory.mktemp("statement-timeout")
    settings=Settings(environment="test",database_url="sqlite:///"+str(tmp_path/"statement.db"),
        command_lock_timeout_ms=50,command_statement_timeout_ms=50,command_write_budget_ms=3000)
    engine=db.make_engine(settings.database_url,settings=settings)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE command_timeout_probe (id integer PRIMARY KEY)"))
        def operation(session):
            session.execute(text("INSERT INTO command_timeout_probe VALUES (1)"))
            session.execute(text("WITH RECURSIVE numbers(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM numbers WHERE x<10000000) SELECT sum(x) FROM numbers"))
        started=time.monotonic()
        with pytest.raises(db.DatabaseWriteTimeout):
            db.retry_write(engine,operation)
        elapsed=time.monotonic()-started
        with engine.connect() as connection:
            during_clean="rota_write" not in connection.info
            count=connection.scalar(text("SELECT count(*) FROM command_timeout_probe"))
            total=connection.scalar(text("WITH RECURSIVE numbers(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM numbers WHERE x<3000) SELECT sum(x) FROM numbers"))
        db.retry_write(engine,lambda session:session.execute(text("INSERT INTO command_timeout_probe VALUES (2)")))
        with engine.connect() as connection:
            after_clean="rota_write" not in connection.info
            busy_timeout=connection.scalar(text("PRAGMA busy_timeout"))
            row=connection.scalar(text("SELECT id FROM command_timeout_probe"))
        return during_clean,count,total,after_clean,busy_timeout,settings.command_lock_timeout_ms,row,elapsed
    finally:
        engine.dispose()


def test_sqlite_statement_timeout_rolls_back_and_cleans_pooled_state(sqlite_statement_timeout):
    during_clean,count,total,after_clean,busy_timeout,expected,row,_=sqlite_statement_timeout
    assert during_clean
    assert count==0
    assert total==4_501_500
    assert after_clean
    assert busy_timeout==expected
    assert row==2


@pytest.mark.wall_benchmark
def test_sqlite_statement_timeout_cpu_budget(sqlite_statement_timeout):
    *_,elapsed=sqlite_statement_timeout
    assert elapsed<1.5


@pytest.mark.parametrize("caller_deadline",[None,.1])
def test_write_budget_spans_repeated_short_statements(tmp_path,monkeypatch,caller_deadline):
    settings=Settings(environment="test",database_url="sqlite:///"+str(tmp_path/"total.db"),
        command_write_budget_ms=100 if caller_deadline is None else 10_000)
    engine=db.make_engine(settings.database_url,settings=settings)
    clock=[0.0]
    completed=[]
    def operation(session):
        for _ in range(20):
            session.execute(text("SELECT 1"))
            completed.append(True)
            clock[0]+=.04
    monkeypatch.setattr(db.time,"monotonic",lambda:clock[0])
    try:
        with pytest.raises(db.DatabaseWriteTimeout):
            db.retry_write(engine,operation,deadline=caller_deadline)
        assert 1<len(completed)<20 and clock[0]>=.1
        with engine.connect() as connection:
            assert "rota_write" not in connection.info
            assert connection.scalar(text("SELECT 1"))==1
    finally:
        engine.dispose()


@pytest.mark.parametrize("name",["command_lock_timeout_ms","command_statement_timeout_ms","command_write_budget_ms","db_pool_timeout_seconds"])
def test_timeout_settings_are_positive_and_loaded_from_environment(name,monkeypatch):
    with pytest.raises(RuntimeError,match="ROTA_"+name.upper()):
        replace(Settings(environment="test"),**{name:0}).check()
    monkeypatch.setenv("ROTA_"+name.upper(),"7")
    assert getattr(Settings.from_env(),name)==7
    ceiling=2_147_483_647//1000 if name=="db_pool_timeout_seconds" else 2_147_483_647
    replace(Settings(environment="test"),**{name:ceiling}).check()
    with pytest.raises(RuntimeError,match="ROTA_"+name.upper()):
        replace(Settings(environment="test"),**{name:ceiling+1}).check()


def test_unavailable_database_message_uses_plain_words_without_promising_rollback(h):
    @h.app.get("/_test/unavailable")
    def unavailable():
        raise OperationalError("SELECT 1",{},Exception("synthetic connection lost"))
    with TestClient(h.app,base_url=h.settings.public_origin) as client:
        response=client.get("/_test/unavailable")
    assert response.status_code==503
    assert response.json()["error"]=={"code":"DATABASE_UNAVAILABLE",
        "message":"The system is unavailable, so we could not confirm whether this change was saved. Try again."}


def test_statement_database_error_is_reported_as_server_error_without_sql(h, caplog):
    @h.app.get("/_test/statement-error")
    def statement_error():
        raise OperationalError("SELECT private_table", {}, Exception("Expression tree is too large; private-value"))
    with caplog.at_level(logging.ERROR), TestClient(h.app,base_url=h.settings.public_origin) as client:
        response=client.get("/_test/statement-error")
    assert response.status_code == 500
    assert response.json()["error"] == {"code": "INTERNAL_ERROR",
        "message": "Something went wrong. Quote the reference if you contact support."}
    assert "private-value" not in response.text
    assert any(record.message == "database_error" for record in caplog.records)


def test_unrecognized_command_kind_is_not_copied_into_logs(h,caplog):
    with caplog.at_level(logging.INFO):
        result=h.command("coordinator","Private arbitrary command kind",status=403)
    assert result["error"]["code"]=="FORBIDDEN"
    assert "Private arbitrary command kind" not in json.dumps([vars(record) for record in caplog.records],default=str)
    kinds=[record.command_kind for record in caplog.records if hasattr(record,"command_kind")]
    assert kinds and set(kinds)=={"unknown"}


@pytest.fixture(scope="module")
def command_rate_limit_timeout(scale_harness):
    h=scale_harness()
    settings=replace(h.settings,command_lock_timeout_ms=50,command_write_budget_ms=200)
    engine=db.make_engine(settings.database_url,settings=settings)
    try:
        with TestClient(create_app(settings,engine),base_url=settings.public_origin) as client:
            client.headers["origin"]=settings.public_origin
            with TestClient(h.app,base_url=settings.public_origin) as login_client:
                login_client.headers["origin"]=settings.public_origin
                login=login_client.post("/auth/login",json={"username":"coordinator","password":TEST_PASSWORD})
                assert login.status_code==200,login.text
                client.cookies.update(login_client.cookies)
            client.headers["x-csrf-token"]=client.cookies["rota_csrf"]
            with engine.connect() as holder:
                holder.execute(text("BEGIN IMMEDIATE"))
                started=time.monotonic()
                response=client.post("/api/commands",headers={"idempotency-key":str(uuid.uuid4())},
                    json={"kind":"notification.ack_all","payload":{}})
                elapsed=time.monotonic()-started
                return login,response,elapsed
    finally:
        engine.dispose()


def test_command_rate_limit_write_is_bounded_before_execute(command_rate_limit_timeout):
    login,response,_=command_rate_limit_timeout
    assert login.status_code==200
    assert response.status_code==503,response.text
    assert response.json()["error"]["code"]=="COMMAND_WRITE_TIMEOUT"


@pytest.mark.wall_benchmark
def test_command_rate_limit_timeout_cpu_budget(command_rate_limit_timeout):
    assert command_rate_limit_timeout[-1]<1


def test_concurrent_commands_wait_for_rate_limit_write_past_sqlite_lock_timeout(h):
    workspace=h.workspace()
    settings=replace(h.settings,command_lock_timeout_ms=200,command_write_budget_ms=5_000)
    engine=db.make_engine(settings.database_url,settings=settings)
    held,waiting,release=Event(),Event(),Event()

    @event.listens_for(engine,"before_cursor_execute")
    def competing_write(connection,cursor,statement,parameters,context,many):
        if held.is_set() and (statement=="BEGIN IMMEDIATE" or statement.startswith("INSERT INTO rate_limits")):
            waiting.set()

    @event.listens_for(engine,"after_cursor_execute")
    def hold_first_command(connection,cursor,statement,parameters,context,many):
        state=connection.info.get("rota_write",{})
        if statement=="BEGIN IMMEDIATE" and state.get("context",{}).get("command_kind")=="shift.create" and not held.is_set():
            held.set()
            assert waiting.wait(10)
            release.wait(.6)  # Deliberately hold the first command beyond the 200 ms SQLite lock timeout.

    try:
        with TestClient(create_app(settings,engine),base_url=settings.public_origin) as client:
            client.headers["origin"]=settings.public_origin
            assert client.post("/auth/login",json={"username":"coordinator","password":TEST_PASSWORD}).status_code==200
            client.headers["x-csrf-token"]=client.cookies["rota_csrf"]
            payload={"workspace_id":workspace["id"],"assignment_type_id":h.seed["type"],
                "location_id":h.seed["locations"][0],"service_ids":[h.seed["services"][0]],
                "start":"2026-10-04T08:00:00+03:00","end":"2026-10-04T12:00:00+03:00","participants":[]}
            def write():
                return client.post("/api/commands",headers={"idempotency-key":str(uuid.uuid4())},
                    json={"kind":"shift.create","payload":payload,"expected_revision":workspace["revision"]})
            with ThreadPoolExecutor(max_workers=2) as executor:
                first=executor.submit(write)
                try:
                    assert held.wait(10)
                    second=executor.submit(write)
                    responses=[first.result(timeout=15),second.result(timeout=15)]
                finally:
                    release.set()
            assert sorted(response.status_code for response in responses)==[200,409], [response.text for response in responses]
            assert next(response for response in responses if response.status_code==409).json()["error"]["code"]=="STALE_REVISION"
            assert len(h.detail(workspace)["shifts"])==1
    finally:
        engine.dispose()


def test_explicit_database_url_does_not_read_unrelated_secrets(monkeypatch,tmp_path):
    monkeypatch.setenv("OIDC_CLIENT_SECRET_FILE",str(tmp_path/"missing-unrelated-secret"))
    engine=db.make_engine("sqlite:///:memory:")
    try:
        assert db.retry_write(engine,lambda session:session.scalar(text("SELECT 1")))==1
    finally:
        engine.dispose()


def test_bulk_transaction_has_statement_limits_without_a_command_total_budget(tmp_path,monkeypatch):
    settings=Settings(command_write_budget_ms=50)
    engine=db.make_engine("sqlite:///"+str(tmp_path/"bulk.db"),settings=settings)
    clock=[0.0]
    monkeypatch.setattr(db.time,"monotonic",lambda:clock[0])
    try:
        with db.transaction(engine,write=True) as session:
            clock[0]=.1
            assert session.scalar(text("SELECT 1"))==1
    finally:
        engine.dispose()
