"""Legacy provenance fails closed and every outbox source has an explicit owner."""
import ast
import logging
from datetime import date, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from apps.api import models as m
from apps.api.db import transaction
from apps.api.services import notification_audience as audience
from apps.worker.main import deliver_notifications
from tests.governance_support import identity
from tests.test_event_delivery import scheduled
from tests.test_publish_test_people_guard import enabled, rename
from packages.domain.validation import check_contracts


BACKENDS = ["sqlite", pytest.param("postgresql", marks=pytest.mark.postgres)]
NEW_SOURCES = ("swap_id", "feedback_id", "feedback_report_id", "staged_id", "availability_id",
               "credential_id", "fact_id", "conversion_id")


@pytest.mark.parametrize("h", BACKENDS, indirect=True)
@pytest.mark.parametrize("provenance", ["test_author", "legacy_unknown"])
@pytest.mark.parametrize("surface", ["content", "catalogue", "history", "inbox"])
def test_links_share_source_privacy_for_every_reader(h, provenance, surface):
    enabled(h, False)
    rename(h, h.seed["identities"]["coordinator"], "Synthetic link author (test)")
    rename(h, h.seed["identities"]["admin"], "Synthetic link viewer (test)")
    identity(h, "real_link_viewer", ["coordinator"])
    rename(h, h.seed["identities"]["real_link_viewer"], "Synthetic real link viewer")
    value = {"name": "Synthetic private link", "kind": "link", "url": "https://example.test/private-link",
        "starts_on": "2020-01-01", "audience": {"everyone": True}}
    if provenance == "test_author":
        link_id = h.configure("department_content", value, role="coordinator")["record"]["id"]
    else:
        with transaction(h.app.state.engine, write=True) as db:
            row = m.DepartmentContent(**{**value, "starts_on": date(2020, 1, 1)},
                department_id=h.seed["department_id"])
            db.add(row)
            db.flush()
            link_id = row.id
    with transaction(h.app.state.engine, write=True) as db:
        source = m.Outbox(department_id=h.seed["department_id"], kind="rule", event_key="synthetic:link",
            payload={"content_id": link_id})
        db.add(source)
        db.flush()
        for role in ("coordinator", "admin", "real_link_viewer"):
            db.add(m.Notification(outbox_id=source.id, identity_id=h.seed["identities"][role],
                title="Synthetic private link", message="Synthetic private link details",
                source_kind="department_content", source_id=link_id))

    def visible(role):
        if surface == "content":
            return link_id in {item["id"] for item in h.get(role, "/api/content")["items"]}
        if surface == "catalogue":
            return link_id in {item["id"] for item in h.get(role,
                "/api/catalogues?include=department_content")["department_content"]}
        if surface == "history":
            response = h.client(role).get(f"/api/catalogues/department_content/{link_id}/history")
            assert response.status_code in {200, 404}, response.text
            return response.status_code == 200
        return link_id in {item["source_id"] for item in h.get(role, "/api/notifications")["notifications"]}

    assert visible("real_link_viewer")
    enabled(h, True)
    assert not visible("real_link_viewer")
    assert visible("coordinator")
    assert visible("admin")
    enabled(h, False)
    assert visible("real_link_viewer")


@pytest.mark.parametrize("h", BACKENDS, indirect=True)
@pytest.mark.parametrize("kind", ["announcement", "event"])
def test_unknown_creator_is_hidden_from_real_people(h, kind):
    enabled(h, False)
    rename(h, h.seed["identities"]["staff"], "Synthetic real viewer", h.seed["people"][0])
    rename(h, h.seed["identities"]["approver"], "Synthetic test viewer (test)")
    if kind == "event":
        source_id, _ = scheduled(h, audience={"everyone": True})
    else:
        with transaction(h.app.state.engine, write=True) as db:
            row = m.DepartmentContent(department_id=h.seed["department_id"], name="Synthetic legacy announcement",
                starts_on=date(2020, 1, 1), kind="announcement", audience={"everyone": True})
            db.add(row)
            db.flush()
            source_id = row.id

    def visible(role):
        path = "/api/content" if kind == "announcement" else "/api/events/occurrences?starts_on=2026-10-01&ends_on=2026-11-01"
        reply = h.get(role, path)
        return source_id in {item["id" if kind == "announcement" else "event_id"]
                            for item in reply["items" if kind == "announcement" else "occurrences"]}

    assert visible("staff")
    enabled(h, True)
    assert not visible("staff")
    assert visible("approver")
    enabled(h, False)
    assert visible("staff")


@pytest.mark.parametrize("h", BACKENDS, indirect=True)
def test_unknown_creator_delivery_fails_closed_and_logs_count(h, caplog):
    enabled(h, True)
    real = h.seed["identities"]["staff"]
    test = h.seed["identities"]["approver"]
    rename(h, real, "Synthetic real recipient")
    rename(h, test, "Synthetic test recipient (test)")
    with transaction(h.app.state.engine, write=True) as db:
        row = m.DepartmentContent(department_id=h.seed["department_id"], name="Synthetic legacy source",
            starts_on=date(2020, 1, 1), kind="announcement")
        db.add(row)
        db.flush()
        source = m.Outbox(department_id=row.department_id, kind="rule", event_key="synthetic:legacy",
            payload={"content_id": row.id})
        db.add(source)
        db.flush()
        caplog.set_level(logging.INFO, logger=audience.__name__)
        deliver_notifications(db, source, roles={real: {"coordinator"}, test: {"approver"}})
        assert set(db.scalars(select(m.Notification.identity_id).where(m.Notification.outbox_id == source.id))) == {test}
        assert audience.filter_people(db, source, [m.Person(id="synthetic-real", display_name="Synthetic real person"),
            m.Person(id="synthetic-test", display_name="Synthetic person (test)")], audit=False)[0].id == "synthetic-test"
        messages = [record.getMessage() for record in caplog.records if record.name == audience.__name__]
        assert any("unknown_source_count=1" in message for message in messages)
        assert all("Synthetic" not in message and row.id not in message for message in messages)
        before = len(messages)
        audience.filter_recipients(db, source, {real, test}, audit=False)
        assert len([r for r in caplog.records if r.name == audience.__name__]) == before
        enabled(h, False)
        assert audience.filter_recipients(db, source, {real, test}) == {real, test}
        source_off = m.Outbox(department_id=row.department_id, kind="rule", event_key="synthetic:legacy:off",
            payload=source.payload)
        db.add(source_off)
        db.flush()
        deliver_notifications(db, source_off, roles={real: {"coordinator"}, test: {"approver"}})
        assert set(db.scalars(select(m.Notification.identity_id).where(m.Notification.outbox_id == source_off.id))) == {real, test}


def authored_source(h, db, key, author, workspace=None):
    values = dict(department_id=h.seed["department_id"])
    if key == "swap_id":
        publication = m.Publication(**values, workspace_id=workspace["id"], sequence=1,
            starts_on=date(2026, 10, 1), ends_on=date(2026, 11, 1), published_by=author,
            snapshot={}, validation={"contracts": check_contracts()}, fingerprint="synthetic")
        db.add(publication)
        db.flush()
        assignment = db.scalar(select(m.Assignment).join(m.Shift).where(m.Shift.workspace_id == workspace["id"]))
        return m.SwapRequest(**values, publication_id=publication.id, proposer_id=author,
            proposer_person_id=h.seed["people"][0], recipient_person_id=h.seed["people"][1],
            first_assignment_id=assignment.id, expires_at=m.now() + timedelta(days=1), reason="Synthetic swap")
    if key in {"feedback_id", "feedback_report_id"}:
        return m.FeedbackReport(**values, reporter_identity_id=author, kind="problem", message="Synthetic report",
            route="#planner", environment="dev", viewport={}, fingerprint="synthetic")
    if key == "staged_id":
        return m.BulkStaged(**values, owner_id=author, payload={}, expires_at=m.now() + timedelta(days=1))
    if key == "availability_id":
        return m.Availability(**values, person_id=h.seed["people"][0], start=m.now(),
            end=m.now() + timedelta(hours=1), kind="recorded_leave", private_reason="Synthetic", recorded_by=author)
    if key == "credential_id":
        row = m.Eligibility(**values, person_id=h.seed["people"][0], service_id=db.scalar(select(m.Service.id).limit(1)),
            starts_on=date(2020, 1, 1))
        db.add(row)
        db.flush()
        from apps.api.services.common import history
        from types import SimpleNamespace
        history(db, SimpleNamespace(identity_id=author, **values), "eligibility.create", row, "Synthetic source")
        return row
    if key == "fact_id":
        return m.SchedulingFact(**values, kind="workload", person_id=h.seed["people"][0],
            proposed_by=author, reason="Synthetic fact", data={})
    assert key == "conversion_id"
    return m.CatalogueConversion(**values, proposed_by=author, reason="Synthetic conversion", input_revision=1,
        plan_hash="synthetic", manifest={}, plan={}, proof={})


@pytest.mark.parametrize("h", BACKENDS, indirect=True)
@pytest.mark.parametrize("key", NEW_SOURCES)
def test_source_author_blocks_real_delivery_even_with_real_actor(h, key):
    enabled(h, True)
    author = h.seed["identities"]["coordinator"]
    real = h.seed["identities"]["staff"]
    test = h.seed["identities"]["approver"]
    rename(h, author, "Synthetic author (test)")
    rename(h, real, "Synthetic real recipient")
    rename(h, test, "Synthetic test recipient (test)")
    workspace = (h.shift(h.workspace(name="Synthetic source rota"), people=[h.seed["people"][0]])["workspace"]
                 if key == "swap_id" else None)
    with transaction(h.app.state.engine, write=True) as db:
        row = authored_source(h, db, key, author, workspace)
        db.add(row)
        db.flush()
        source = m.Outbox(department_id=row.department_id, kind="rule", event_key="synthetic:" + key,
            payload={key: row.id, "actor_id": h.seed["identities"]["admin"]})
        db.add(source)
        db.flush()
        deliver_notifications(db, source, roles={real: {"coordinator"}, test: {"approver"}})
        delivered = set(db.scalars(select(m.Notification.identity_id).where(m.Notification.outbox_id == source.id)))
        assert delivered == {test}
        db.get(m.Identity, author).display_name = "Synthetic real author"
        db.flush()
        assert audience.filter_recipients(db, source, {real, test}, audit=False) == {real, test}


WRITER_PAYLOAD_ALLOW = {
    "apps/api/services/common.py:outbox":
        "Only **payload forwards the wrapper parameter; every outbox caller is scanned separately.",
}


def writer_source_keys_in_source(source, path="synthetic.py"):
    """Resolve ORM/Core/bulk writer payloads; unknown syntax cannot silently lose source keys."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    aliases = {alias.asname or alias.name: alias.name for node in ast.walk(tree)
               if isinstance(node, ast.ImportFrom) for alias in node.names}
    found = {}
    scopes = {}

    def scope(node):
        trail = []
        while node not in scopes and not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef)):
            trail.append(node)
            node = parents[node]
        owner = scopes.get(node, node)
        scopes.update((child, owner) for child in trail)
        return owner

    bindings = {}
    for entry in ast.walk(tree):
        if isinstance(entry, (ast.Assign, ast.AnnAssign)):
            for target in entry.targets if isinstance(entry, ast.Assign) else [entry.target]:
                if isinstance(target, ast.Name):
                    owner = scope(entry)
                    bindings.setdefault((owner, target.id), []).append(
                        (entry.lineno, entry.value if parents[entry] is owner else None))

    def bound(node, seen=()):
        if not isinstance(node, ast.Name) or node.id in seen:
            return node
        owner = scope(node)
        while True:
            definitions = [value for line, value in bindings.get((owner, node.id), ()) if line < node.lineno]
            if definitions:
                return bound(definitions[0], (*seen, node.id)) if len(definitions) == 1 else node
            if isinstance(owner, ast.Module) or node.id in {arg.arg for arg in ast.walk(owner.args) if isinstance(arg, ast.arg)}:
                return node
            owner = scope(parents[owner])

    def name(node):
        return aliases.get(node.id, node.id) if isinstance(node, ast.Name) else node.attr if isinstance(node, ast.Attribute) else ""

    def is_outbox(node):
        node = bound(node)
        return is_outbox(node.value) if isinstance(node, ast.Attribute) and node.attr == "__table__" else name(node) == "Outbox"

    def require(condition, writer):
        assert condition, f"{path}:{writer.lineno}: unresolved writer payload"

    def literal_keys(node, writer, *, unpack=False):
        original = node
        node = bound(node)
        if unpack and isinstance(node, ast.Name) and node.id == "payload" and (
                f"{path}:{getattr(scope(writer), 'name', '')}" in WRITER_PAYLOAD_ALLOW):
            return set()
        if isinstance(node, ast.IfExp):
            return literal_keys(node.body, writer) | literal_keys(node.orelse, writer)
        require(isinstance(node, ast.Dict), writer)
        keys = set()
        for key, value in zip(node.keys, node.values):
            if key is None:
                keys.update(literal_keys(value, writer, unpack=True))
            else:
                require(isinstance(key, ast.Constant) and isinstance(key.value, str), writer)
                keys.add(key.value)
                for nested in ast.walk(value):
                    if isinstance(nested, ast.Dict):
                        keys.update(literal_keys(nested, writer))
        if isinstance(original, ast.Name):
            for mutation in ast.walk(scope(original)):
                if scope(mutation) is not scope(original) or getattr(mutation, "lineno", writer.lineno) >= writer.lineno:
                    continue
                if isinstance(mutation, ast.Subscript) and isinstance(mutation.ctx, ast.Store) and (
                        isinstance(mutation.value, ast.Name) and mutation.value.id == original.id):
                    require(isinstance(mutation.slice, ast.Constant) and isinstance(mutation.slice.value, str), writer)
                    keys.add(mutation.slice.value)
                if isinstance(mutation, ast.Call) and isinstance(mutation.func, ast.Attribute) and (
                        isinstance(mutation.func.value, ast.Name) and mutation.func.value.id == original.id
                        and mutation.func.attr == "update"):
                    for argument in mutation.args:
                        keys.update(literal_keys(argument, writer))
                    require(all(keyword.arg for keyword in mutation.keywords), writer)
                    keys.update(keyword.arg for keyword in mutation.keywords)
        return keys

    def record(payload, writer):
        for key in literal_keys(payload, writer):
            if key.endswith(("_id", "_ids")):
                found.setdefault(key, set()).add(f"{path}:{writer.lineno}")

    def rows_payloads(rows, writer):
        rows = bound(rows)
        if isinstance(rows, (ast.List, ast.Tuple)):
            for row in rows.elts:
                rows_payloads(row, writer)
            return
        require(isinstance(rows, ast.Dict), writer)
        require(all(isinstance(key, ast.Constant) and isinstance(key.value, str) for key in rows.keys), writer)
        payloads = [value for key, value in zip(rows.keys, rows.values) if key.value == "payload"]
        require(payloads, writer)
        for payload in payloads:
            record(payload, writer)

    def insert_root(node):
        node = bound(node)
        if not isinstance(node, ast.Call):
            return None
        target = node.args[0] if node.args else next((kw.value for kw in node.keywords if kw.arg == "table"), None)
        if name(node.func) == "insert" and (is_outbox(target) or (
                isinstance(node.func, ast.Attribute) and is_outbox(node.func.value))):
            return node
        return insert_root(node.func.value) if isinstance(node.func, ast.Attribute) else None

    def statement_payloads(statement, writer, rows=None):
        node = bound(statement)
        resolved = False
        while isinstance(node, ast.Call):
            if name(node.func) == "values":
                require(all(keyword.arg for keyword in node.keywords), writer)
                for keyword in node.keywords:
                    if keyword.arg == "payload":
                        record(keyword.value, writer)
                        resolved = True
                for argument in node.args:
                    rows_payloads(argument, writer)
                    resolved = True
            node = bound(node.func.value) if isinstance(node.func, ast.Attribute) else None
        if rows is not None:
            rows_payloads(rows, writer)
            resolved = True
        require(resolved, writer)

    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    handled = set()
    for call in calls:
        kind = name(call.func)
        if kind in {"outbox", "Outbox"}:
            require(all(keyword.arg for keyword in call.keywords), call)
            payload = next((keyword.value for keyword in call.keywords if keyword.arg == "payload"), None)
            if kind == "outbox" and payload is None and len(call.args) > 4:
                payload = call.args[4]
            record(payload, call)
        if kind in {"execute", "executemany"} and call.args and (insert := insert_root(call.args[0])):
            rows = call.args[1] if len(call.args) > 1 else next((kw.value for kw in call.keywords if kw.arg in {"parameters", "params"}), None)
            statement_payloads(call.args[0], call, rows)
            handled.add(insert)
        if kind in {"bulk_insert", "bulk_insert_mappings"}:
            target = call.args[0] if call.args else next((kw.value for kw in call.keywords if kw.arg in {"table", "mapper"}), None)
            if is_outbox(target):
                rows = call.args[1] if len(call.args) > 1 else next((kw.value for kw in call.keywords if kw.arg in {"rows", "mappings"}), None)
                rows_payloads(rows, call)
    for call in calls:
        if insert_root(call) is not call or call in handled:
            continue
        statement = call
        while isinstance(parents.get(statement), ast.Attribute) and isinstance(parents.get(parents[statement]), ast.Call):
            statement = parents[parents[statement]]
        statement_payloads(statement, call)
    return found


def writer_source_keys(root):
    found = {}
    for folder in ("apps", "scripts"):
        for path in sorted((root / folder).rglob("*.py")):
            for key, locations in writer_source_keys_in_source(
                    path.read_text(encoding="utf-8-sig"), path.relative_to(root).as_posix()).items():
                found.setdefault(key, set()).update(locations)
    return found


def test_every_outbox_writer_source_is_classified():
    found = writer_source_keys(Path(__file__).resolve().parents[1])
    sources = getattr(audience, "OUTBOX_SOURCES", {})
    exempt = getattr(audience, "NEVER_TEST_AUTHORED", {})
    assert not set(sources) & set(exempt)
    assert all(exempt.values()), "Every non-source key needs its reason"
    assert not (missing := found.keys() - sources.keys() - exempt.keys()), {key: sorted(found[key]) for key in missing}


def test_writer_discovery_rejects_a_new_unclassified_source():
    source = "def write():\n    outbox(db, actor, 'key', 'kind', {'new_source_id': row.id})\n"
    assert set(writer_source_keys_in_source(source)) == {"new_source_id"}


@pytest.mark.parametrize("payload", ["make_payload()", "{key: value for key, value in pairs}",
    "payload", "{**opaque}", "{key: row.id}", "{42: row.id}"])
def test_writer_discovery_rejects_unresolved_payloads(payload):
    source = f"def write(payload):\n    outbox(db, actor, 'key', 'kind', {payload})\n"
    with pytest.raises(AssertionError, match=r"synthetic.py:2: unresolved writer payload"):
        writer_source_keys_in_source(source)


@pytest.mark.parametrize("setup", [
    "payload = make_payload()",
    "if flag:\n        payload = {}",
    "payload = {}\n    payload.update(opaque)",
    "payload = {}\n    payload[key] = row.id",
])
def test_writer_discovery_rejects_unresolved_local_payloads(setup):
    source = f"def write(payload):\n    {setup}\n    outbox(db, actor, 'key', 'kind', payload)\n"
    with pytest.raises(AssertionError, match=r"synthetic.py:\d+: unresolved writer payload"):
        writer_source_keys_in_source(source)


@pytest.mark.parametrize("statement", [
    "db.execute(insert(m.Outbox).values(payload={'new_source_id': row.id}))",
    "db.execute(sa.insert(Outbox), [{'payload': {'new_source_id': row.id}}])",
    "db.execute(m.Outbox.__table__.insert(), [{'payload': {'new_source_id': row.id}}])",
    "db.execute(sa.insert(Outbox.__table__).values([{'payload': {'new_source_id': row.id}}]))",
    "db.bulk_insert_mappings(Outbox, [{'payload': {'new_source_id': row.id}}])",
    "op.bulk_insert(m.Outbox.__table__, [{'payload': {'new_source_id': row.id}}])",
    "db.bulk_save_objects([Outbox(payload={'new_source_id': row.id})])",
    "stmt = sa.insert(Outbox)\n    rows = [{'payload': {'new_source_id': row.id}}]\n    db.execute(stmt, rows)",
    "table = m.Outbox.__table__\n    db.execute(table.insert().values(payload={'new_source_id': row.id}))",
    "db.execute(sql_insert(OB), [{'payload': {'new_source_id': row.id}}])",
    "db.execute(sa.insert(table=Outbox).values(payload={'new_source_id': row.id}))",
    "db.bulk_insert_mappings(mapper=Outbox, mappings=[{'payload': {'new_source_id': row.id}}])",
])
def test_writer_discovery_covers_core_and_bulk_inserts(statement):
    source = "from sqlalchemy import insert as sql_insert\nfrom apps.api.models import Outbox as OB\ndef write():\n    " + statement + "\n"
    assert set(writer_source_keys_in_source(source)) == {"new_source_id"}


@pytest.mark.parametrize("statement", [
    "db.execute(insert(Outbox).values(payload=make_payload()))",
    "db.execute(insert(Outbox), make_rows())",
    "db.execute(Outbox.__table__.insert(), [{'payload': {**opaque}}])",
    "db.bulk_insert_mappings(Outbox, [{'payload': {key: row.id}}])",
    "op.bulk_insert(Outbox.__table__, rows)",
    "db.execute(insert(Outbox).from_select(['payload'], query))",
    "db.execute(sa.insert(table=Outbox).values(payload=make_payload()))",
    "db.bulk_insert_mappings(mapper=Outbox, mappings=make_rows())",
])
def test_core_and_bulk_writers_reject_unresolved_payloads(statement):
    with pytest.raises(AssertionError, match=r"synthetic.py:2: unresolved writer payload"):
        writer_source_keys_in_source("def write(rows):\n    " + statement + "\n")


def test_forwarding_allow_entry_does_not_hide_another_unresolved_writer():
    source = "def outbox(payload):\n    m.Outbox(payload={**payload, 'actor_id': actor.id})\n    m.Outbox(payload=make_payload())\n"
    with pytest.raises(AssertionError, match=r"apps/api/services/common.py:3: unresolved writer payload"):
        writer_source_keys_in_source(source, "apps/api/services/common.py")
